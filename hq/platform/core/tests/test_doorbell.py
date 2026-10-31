"""A capability's write wakes the doorbell's thread, which asks whether a pass
is due, so the request that wrote pays for none of the asking."""

import threading
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from hq.domains.control_plane.tests.test_moved_settings_apply import PROXY, _Estate
from hq.platform.application import cadence
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.security import cli_principal
from hq.platform.core import doorbell

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
