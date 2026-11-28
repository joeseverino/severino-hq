"""What a run's playbook stated beside its result (``RunStated``) is kept on
the run, struck of secrets, only when it keeps to the bridge contract's bounds
and names only the run's own machines; the run's row and its read carry
each reading it stated with its records. Every procedure here is a synthetic
``example.*`` one."""

from unittest import mock

from hq.domains.control_plane.models import OperationRequest

from ..controller import ControllerReport, claim_next_operation, renew_operation, report_operation
from .procedures import APP
from .test_work_runs import WORKER, WorkRunsTestCase

FILESYSTEM = {"mount": "/", "free_bytes": 1024, "total_bytes": 4096, "read_at": "2026-11-13T12:00:00Z"}
STATED = {
    "readings": {APP: {"host.filesystem": [FILESYSTEM], "host.reboot": [{"required": False}]}},
    "preview": {APP: ["would write /etc/example.conf"]},
}


class RunStatedTestCase(WorkRunsTestCase):
    def report(self, run: OperationRequest, stated, **fields) -> dict:
        return report_operation(
            str(run.pk),
            ControllerReport(
                success=True,
                observed_generation=run.input["generation"],
                stated=stated,
                **{"message": "Done.", **fields},
            ),
            controller_id=WORKER,
        )

    def refused(self, stated, *hosts) -> None:
        """A claimed run on ``hosts`` whose report stating ``stated`` HQ refuses, leaving it claimed."""

        run = self.claimed(self.run_of(*(hosts or (APP,))))
        with self.assertRaises(ValueError):
            self.report(run, stated)
        run.refresh_from_db()
        self.assertEqual(run.state, OperationRequest.State.CLAIMED)
        self.assertNotIn("stated", run.result or {})
        # Ended, so the next case's run is not held behind it.
        self.report(run, None)


class BoundsTests(RunStatedTestCase):
    """Each bound of ``RunStated`` refuses the report whole: nothing is cut down."""

    def test_each_bound_and_each_shape_is_refused(self):
        long = "x" * 501
        cases = {
            "more records of one kind than the bound": {"readings": {APP: {"host.filesystem": [FILESYSTEM] * 201}}},
            "a value longer than a text": {"readings": {APP: {"host.filesystem": [{**FILESYSTEM, "mount": long}]}}},
            "a nested value longer than a text": {"readings": {APP: {"host.reboot": [{"packages": [long]}]}}},
            "a member name longer than a text": {"readings": {APP: {"host.filesystem": [{long: 1}]}}},
            "more preview lines than the bound": {"preview": {APP: ["line"] * 201}},
            "a preview line longer than a text": {"preview": {APP: [long]}},
            "a kind longer than its bound": {"readings": {APP: {"host." + "x" * 96: [FILESYSTEM]}}},
            "a record that is not an object": {"readings": {APP: {"host.filesystem": ["/"]}}},
            "a preview line that is not text": {"preview": {APP: [1]}},
            "a stat the contract does not name": {"readings": {}, "extra": {APP: "x"}},
            "more bytes in all than the bound": {
                "readings": {
                    APP: {f"host.part{index}": [{"text": "x" * 400} for _ in range(10)] for index in range(20)}
                }
            },
        }
        for case, stated in cases.items():
            with self.subTest(case):
                self.refused(stated)


class NotARunTests(RunStatedTestCase):
    def test_only_a_run_reports_what_its_playbook_stated(self):
        from ..automation_sources import queue_scan
        from ..security import internal_principal

        with mock.patch("hq.platform.application.cadence._ring"), self.captureOnCommitCallbacks(execute=True):
            scan = queue_scan(principal=internal_principal("controller"), once="example-scan")
        claim = claim_next_operation(WORKER, capabilities=(("automation.source", "scan"),))["operation"]
        self.assertEqual(claim["id"], str(scan.pk))
        scan.refresh_from_db()

        with self.assertRaises(ValueError):
            report_operation(
                str(scan.pk),
                ControllerReport(success=False, observed_generation=scan.input["generation"], stated=STATED),
                controller_id=WORKER,
            )


class HeadlineTests(RunStatedTestCase):
    """A run that ended well says what it did, on every list and read of
    work: what its readings say, else how its tasks ended."""

    def headline(self, run: OperationRequest) -> str:
        """Its note, which its row, its page and the read's ``headline`` all say."""

        (found,) = [row for row in self.listed() if row.id == str(run.pk)]
        return found.note

    def test_a_run_that_stated_a_release_says_the_app_it_released(self):
        run = self.claimed(self.run_of(APP))
        released = {"project": "example-app", "result": "ok", "health": "healthy", "changed": True}
        left = {"project": "example-app-left-alone", "result": "ok", "health": "healthy", "changed": False}

        self.report(run, {"readings": {APP: {"apps.pull": [released, left]}}})

        said = self.headline(run)
        self.assertIn("example-app", said)
        self.assertIn("healthy", said)
        self.assertNotIn("example-app-left-alone", said)
        self.assertNotIn("Done", said)

    def test_a_run_that_stated_nothing_says_how_its_tasks_ended(self):
        run = self.claimed(self.run_of(APP))
        events = [
            {"host": APP, "task": "Read the service", "outcome": "ok", "message": ""},
            {"host": APP, "task": "Restart the service", "outcome": "skipped", "message": ""},
        ]
        renew_operation(str(run.pk), controller_id=WORKER, events=events)

        self.report(run, None, message="")

        said = self.headline(run)
        self.assertIn("1 ok", said)
        self.assertIn("1 skipped", said)
