"""The doorbell's thread asks whether a pass is due as a write it reads
commits, whoever wrote it, so the request that wrote pays for none of the
asking and work queued anywhere starts its pass at once."""

import threading
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from hq.domains.control_plane.models import OperationRequest
from hq.domains.control_plane.tests.test_moved_settings_apply import PROXY, _Estate
from hq.platform.application import cadence
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.test_cadence import swept
from hq.platform.core import ahead, doorbell
from hq.platform.core.testing import declare

# A bound on a wait for the other thread, never a pause: an ask that is coming
# arrives long before it.
ARRIVES_WITHIN = 30.0


class AWriteAsksOffTheRequestThreadTests(_Estate):
    def test_a_capability_write_runs_none_of_pass_dues_queries(self):
        request = threading.current_thread()
        asked_here: list[dict] = []
        real = cadence.pass_due

        def pass_due():
            if threading.current_thread() is not request:
                return real()
            with CaptureQueriesContext(connection) as queries:
                answer = real()
            asked_here.extend(queries.captured_queries)
            return answer

        woken = threading.Event()
        with (
            mock.patch.object(cadence, "pass_due", pass_due),
            # The thread's own ask would read this test's rows on a connection
            # that cannot see them; that it asks is the promise here.
            mock.patch.object(doorbell, "ask", side_effect=woken.set),
            doorbell.ringing_when_due(),
        ):
            # The ask the thread starts with.
            self.assertTrue(woken.wait(ARRIVES_WITHIN), "The doorbell's thread did not ask as it started.")
            woken.clear()
            with self.captureOnCommitCallbacks(execute=True):
                result = execute_capability(
                    "infrastructure.resource.create",
                    {"key": "example-proxy", "kind": "npm.proxy_host", "spec": dict(PROXY)},
                    principal=cli_principal(),
                )
            self.assertTrue(result["ok"], result)
            self.assertEqual(asked_here, [])
            # Inside the doorbell's lifetime: stopping it would end the thread
            # before an ask it had not reached yet.
            self.assertTrue(woken.wait(ARRIVES_WITHIN), "The write did not wake the doorbell's thread.")


class AnyWriterRingsTests(TransactionTestCase):
    def test_work_queued_by_a_writer_that_is_no_capability_rings_at_once(self):
        # Read a minute ago: no sweep is due, so only the queued work rings.
        swept(age_seconds=60)
        spec = {"domain": "app.example.com", "answer": "192.0.2.1"}
        resource = declare("example-rewrite", "adguard.rewrite", spec, observed_generation=1)
        asked, rung = threading.Event(), threading.Event()
        real = doorbell.ask

        def ask():
            try:
                return real()
            finally:
                asked.set()

        with (
            mock.patch.object(cadence, "_ring", side_effect=lambda _doorbell: rung.set() or True),
            mock.patch.object(doorbell, "ask", ask),
            ahead.keeping(),
            doorbell.ringing_when_due(),
        ):
            self.assertTrue(asked.wait(ARRIVES_WITHIN), "The doorbell's thread did not ask as it started.")
            self.assertFalse(rung.is_set())
            # As a bridge report queues a follow-on: written, with no capability around it.
            OperationRequest.objects.create(
                resource=resource, action=OperationRequest.Action.RECONCILE, idempotency_key="example-follow-on"
            )
            self.assertTrue(rung.wait(ARRIVES_WITHIN), "Work queued outside a capability did not ring.")
