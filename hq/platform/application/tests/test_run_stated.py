"""What a run's playbook stated beside its result (``RunStated``) is kept on
the run, struck of secrets, only when it keeps to the bridge contract's bounds
and names only the run's own machines; the run's row and its read name the
kinds of reading it stated. Every procedure here is a synthetic ``example.*``
one."""

from hq.domains.control_plane.models import OperationRequest

from ..controller import ControllerReport, claim_next_operation, report_operation
from ..run_reads import get_run, list_runs
from . import test_work_runs
from .procedures import APP, DB
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
                success=True, observed_generation=run.input["generation"], message="Done.", stated=stated, **fields
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


class KeptTests(RunStatedTestCase):
    def test_what_a_run_s_playbook_stated_is_kept_on_the_run_as_stated(self):
        run = self.claimed(self.run_of(APP))

        self.report(run, STATED)

        run.refresh_from_db()
        self.assertEqual(run.state, OperationRequest.State.SUCCEEDED)
        self.assertEqual(run.result["stated"], STATED)

    def test_the_run_s_row_and_its_read_name_each_kind_it_stated_once(self):
        both = {"readings": {APP: STATED["readings"][APP], DB: {"host.filesystem": [FILESYSTEM]}}}
        run = self.claimed(self.run_of(APP, DB))

        self.report(run, both)

        (row,) = self.listed()
        self.assertEqual(row.evidence.stated, ("host.filesystem", "host.reboot"))
        self.assertEqual(get_run(str(run.pk))["stated"], ["host.filesystem", "host.reboot"])
        self.assertEqual(list_runs()["items"][0]["stated"], ["host.filesystem", "host.reboot"])

    def test_a_run_that_stated_nothing_names_no_kind(self):
        run = self.claimed(self.run_of(APP))

        self.report(run, None)

        run.refresh_from_db()
        self.assertNotIn("stated", run.result)
        self.assertEqual(get_run(str(run.pk))["stated"], [])


class SecretTests(RunStatedTestCase):
    def test_a_secret_in_what_a_playbook_stated_is_struck_as_it_settles(self):
        token = "ghp_" + "A1b2C3d4E5" * 4
        stated = {
            "readings": {APP: {"host.filesystem": [{**FILESYSTEM, "mount": f"/{token}", "password": "hunter22"}]}},
            "preview": {APP: [f"would write token {token}"]},
        }
        run = self.claimed(self.run_of(APP))

        self.report(run, stated)

        run.refresh_from_db()
        kept = run.result["stated"]
        (record,) = kept["readings"][APP]["host.filesystem"]
        self.assertNotIn(token, str(kept))
        self.assertNotIn("hunter22", str(kept))
        self.assertEqual(record["free_bytes"], FILESYSTEM["free_bytes"])


class OtherMachineTests(RunStatedTestCase):
    def test_a_reading_for_a_machine_the_run_is_not_on_is_refused(self):
        self.refused({"readings": {DB: {"host.filesystem": [FILESYSTEM]}}})

    def test_a_preview_for_a_machine_the_run_is_not_on_is_refused(self):
        self.refused({"preview": {APP: ["fine"], DB: ["would write /etc/example.conf"]}})


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

    def test_a_statement_at_every_bound_is_kept(self):
        at_bounds = {
            "readings": {APP: {"host.filesystem": [{**FILESYSTEM, "mount": "/" + "x" * 499}] * 100}},
            "preview": {APP: ["x" * 500] * 10},
        }
        run = self.claimed(self.run_of(APP))

        self.report(run, at_bounds)

        run.refresh_from_db()
        self.assertEqual(run.result["stated"], at_bounds)


class NotARunTests(RunStatedTestCase):
    scanned = test_work_runs.ScanTests.scanned

    def test_only_a_run_reports_what_its_playbook_stated(self):
        scan = self.scanned()
        claim = claim_next_operation(WORKER, capabilities=(("automation.source", "scan"),))["operation"]
        self.assertEqual(claim["id"], str(scan.pk))
        scan.refresh_from_db()

        with self.assertRaises(ValueError):
            report_operation(
                str(scan.pk),
                ControllerReport(success=False, observed_generation=scan.input["generation"], stated=STATED),
                controller_id=WORKER,
            )
