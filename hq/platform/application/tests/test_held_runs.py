"""A run whose playbook hands off to a unit it leaves running is held, and
settled only by the read its procedure names, asked when that unit's job on
HQ's machine ends or once its bound passes: an event says when to look, never
what is true. Every procedure here is a synthetic ``example.*`` one."""

from datetime import timedelta
from itertools import count
from typing import Annotated
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from pydantic import Field

from hq.domains.control_plane.models import InboundEvent, InboundSource, OperationRequest
from hq.platform.application.contracts import Closed
from hq.platform.core.testing import AGENT

from .. import outbound_work
from ..controller import ControllerReport, claim_next_operation, release_lapsed, report_operation
from ..inbound import apply_events
from ..machines import Machine
from .procedures import APP, DB, Supply, approved_run, manifest

PULL, LAST_PULL = "example.pull", "example.last_pull"
COMMIT = "0123abcd" * 5
UNIT = f"example-pull@{COMMIT}.service"
OTHER = "example-other.service"
WORKER = "example-worker"
CLAIMS = (("automation.operation", "run"),)
RECEIPT = {APP: {"host.reboot": [{"required": False}]}}
_DELIVERIES = count()


class AtRevision(Closed):
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", description="The commit.")]


class HeldRunTests(TestCase):
    def setUp(self):
        self.supply = Supply(
            self,
            manifest(id=PULL, input_type=AtRevision, settled_by=LAST_PULL, retry="safe", timeout_seconds=1500),
            manifest(
                id=LAST_PULL,
                input_type=AtRevision,
                effect="read",
                retry="safe",
                timeout_seconds=60,
                preconditions=(),
                verification=(),
            ),
        )
        here = mock.patch("hq.platform.application.host_work.own_machine", return_value=Machine(APP))
        here.start()
        self.addCleanup(here.stop)
        self.succeeded = mock.patch("hq.platform.application.controller.succeeded").start()
        self.addCleanup(mock.patch.stopall)

    def pull(self, *hosts: str) -> OperationRequest:
        answer = approved_run(
            self,
            {"hosts": list(hosts or (APP,)), "parameters": {"revision": COMMIT}},
            principal=AGENT,
            key=f"example-pull-{next(_DELIVERIES)}",
            name=f"automation.{PULL}.run",
        )
        self.assertTrue(answer["ok"], answer)
        return OperationRequest.objects.get(pk=answer["operation"]["id"])

    def claim(self) -> dict:
        return claim_next_operation(WORKER, capabilities=CLAIMS)

    def report(self, run: OperationRequest, stated=None, *, success=True) -> None:
        report_operation(
            str(run.pk),
            ControllerReport(
                success=success, observed_generation=run.input["generation"], message="Done.", stated=stated
            ),
            controller_id=WORKER,
        )

    def held(self) -> OperationRequest:
        run = self.pull()
        self.claim()
        self.report(run, {"handoff": {APP: UNIT}})
        run.refresh_from_db()
        return run

    def job_ended(self, unit: str = UNIT, result: str = "done", **batch) -> mock.MagicMock:
        """The end of ``unit``'s job on HQ's machine, as its watcher tells it."""

        event = {
            "provider": "host",
            "event": f"unit.job.{result}",
            "subject": unit,
            "delivery_id": f"example-{next(_DELIVERIES)}",
            "received_at": timezone.now().isoformat(),
        }
        with (
            mock.patch.object(outbound_work, "start_for_host", side_effect=outbound_work.run_for_host) as start,
            self.captureOnCommitCallbacks(execute=True),
        ):
            apply_events({"source": InboundSource.HOST, "events": [event], **batch})
        return start

    def stored(self, unit: str):
        return InboundEvent.objects.filter(subject=unit)

    def reads(self, held: OperationRequest):
        return OperationRequest.objects.filter(input__settles=str(held.pk))

    def read_ends(self, held: OperationRequest, stated=None, *, success=True) -> None:
        """The read that settles ``held`` is claimed and ends, stating ``stated``."""

        claim = self.claim()
        (read,) = self.reads(held).filter(state=OperationRequest.State.CLAIMED)
        self.assertEqual(claim["operation"]["id"], str(read.pk))
        self.assertNotIn("settles", claim["input"])
        self.report(read, stated, success=success)

    def test_a_hand_off_holds_the_run_under_its_declared_bound(self):
        run = self.held()

        self.assertEqual(run.state, OperationRequest.State.CLAIMED)
        self.assertEqual(run.result["handoff"], {APP: UNIT})
        self.assertEqual(run.lease_expires_at, run.claimed_at + timedelta(seconds=1500))
        self.succeeded.assert_not_called()

    def test_a_job_s_end_settles_the_held_run_from_its_read(self):
        run = self.held()

        self.job_ended()
        self.read_ends(run, {"readings": RECEIPT})

        run.refresh_from_db()
        self.assertEqual(run.state, OperationRequest.State.SUCCEEDED)
        self.assertEqual(run.result["stated"], {"readings": RECEIPT})
        self.assertEqual(run.result["handoff"], {APP: UNIT})
        self.succeeded.assert_any_call(run)

    def test_a_read_whose_checks_fail_what_it_found_fails_the_held_run(self):
        run = self.held()
        rolled_back = {APP: {"example.release": [{"project": "example-web", "result": "rolled_back"}]}}

        self.job_ended()
        self.read_ends(run, {"readings": rolled_back}, success=False)

        run.refresh_from_db()
        self.assertEqual(run.state, OperationRequest.State.FAILED)
        self.assertEqual(run.result["stated"], {"readings": rolled_back})
        self.assertNotIn(mock.call(run), self.succeeded.call_args_list)

    def test_only_its_own_unit_s_end_asks_the_read_and_another_unit_s_end_is_not_stored(self):
        run = self.held()

        self.job_ended(OTHER)
        self.assertFalse(self.reads(run).exists())
        self.assertFalse(self.stored(OTHER).exists())

        self.job_ended()
        self.read_ends(run, {"readings": RECEIPT})

        run.refresh_from_db()
        self.assertEqual(run.state, OperationRequest.State.SUCCEEDED)
        self.assertEqual(self.reads(run).count(), 1)

    def test_a_read_that_found_the_unit_running_is_not_asked_again_by_another_unit_s_end(self):
        run = self.held()

        self.job_ended()
        self.read_ends(run)
        self.job_ended(OTHER)
        self.job_ended(OTHER, "failed")

        self.assertEqual(self.reads(run).count(), 1)
        # A failed job is still told, for the machine's units alone.
        self.assertEqual(list(self.stored(OTHER).values_list("reads", flat=True)), [["host.unit"]])

    def test_a_pull_that_ended_before_its_hand_off_settles_once_the_hold_is_stored(self):
        run = self.pull()
        self.claim()

        self.job_ended()
        with (
            mock.patch.object(outbound_work, "start_for_host", side_effect=outbound_work.run_for_host),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.report(run, {"handoff": {APP: UNIT}})
        self.read_ends(run, {"readings": RECEIPT})

        run.refresh_from_db()
        self.assertEqual(run.state, OperationRequest.State.SUCCEEDED)

    def test_forged_events_put_one_read_in_flight_and_settle_nothing(self):
        run = self.held()

        for _ in range(3):
            self.job_ended()

        self.assertEqual(self.reads(run).count(), 1)
        self.read_ends(run)
        run.refresh_from_db()
        self.assertEqual(run.state, OperationRequest.State.CLAIMED)
        self.assertNotIn(mock.call(run), self.succeeded.call_args_list)

    def test_with_no_event_the_bound_asks_the_read_once_and_ends_by_what_it_finds(self):
        for stated, ended in ((None, OperationRequest.State.CANCELLED), ({"readings": RECEIPT}, "succeeded")):
            with self.subTest(stated=stated):
                run = self.held()
                OperationRequest.objects.filter(pk=run.pk).update(
                    lease_expires_at=timezone.now() - timedelta(seconds=1)
                )

                release_lapsed(timezone.now())
                release_lapsed(timezone.now())
                self.assertEqual(self.reads(run).count(), 1)
                self.read_ends(run, stated)

                run.refresh_from_db()
                self.assertEqual(run.state, ended)
                if stated is None:
                    self.assertEqual(run.result["reason"], "OutcomeUnknown")

    def test_a_job_s_end_with_no_run_held_starts_nothing_and_is_not_stored(self):
        self.job_ended().assert_not_called()
        self.assertFalse(self.stored(UNIT).exists())

    def test_a_held_run_on_another_machine_waits_for_its_bound(self):
        run = self.pull(DB)
        self.claim()
        self.report(run, {"handoff": {DB: UNIT}})

        self.job_ended()

        self.assertFalse(self.reads(run).exists())

    def test_a_held_run_says_the_unit_it_waits_on_and_what_each_app_s_containers_did_since(self):
        from ..work_runs import run_rows, seen_as_runs

        run = self.held()
        for container, event in (("example-web-1", "start"), ("example-db-1", "die"), ("example-other-1", "start")):
            arrived = {
                "provider": "host",
                "event": f"docker.container.{event}",
                "subject": container,
                "delivery_id": f"example-{next(_DELIVERIES)}",
                "received_at": timezone.now().isoformat(),
            }
            with mock.patch.object(outbound_work, "start_for_host"), self.captureOnCommitCallbacks(execute=True):
                apply_events({"source": InboundSource.HOST, "events": [arrived]})
        stacks = {(APP, "example-web-1"): "example-web", (APP, "example-db-1"): "example-db"}

        with (
            mock.patch("hq.platform.application.work_runs.stacks", return_value=stacks),
            mock.patch("hq.platform.application.work_runs.own_machine", return_value=Machine(APP)),
        ):
            (row,) = run_rows(seen_as_runs(OperationRequest.objects.filter(pk=run.pk)))

        for fact in (UNIT.partition("@")[0], APP, "example-web-1: started", "example-db-1: stopped"):
            self.assertIn(fact, row.note)
        self.assertNotIn("example-other", row.note)

    def test_a_hand_off_naming_a_machine_the_run_is_not_on_is_refused(self):
        run = self.pull()
        self.claim()

        with self.assertRaises(ValueError):
            self.report(run, {"handoff": {DB: UNIT}})

    def test_a_hand_off_from_a_procedure_that_names_no_settling_read_is_refused(self):
        self.supply(manifest(id=PULL, input_type=AtRevision, retry="safe"), *self.supply.manifests[1:])
        run = self.pull()
        self.claim()

        with self.assertRaises(ValueError):
            self.report(run, {"handoff": {APP: UNIT}})
