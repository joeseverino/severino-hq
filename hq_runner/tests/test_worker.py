"""A pass of the worker runs only what its claim binds it to, holds the run's
lease while it runs, stops when HQ or the timeout says so, never runs a run a
crash may have started, and reports a bounded outcome no secret reaches; a
scan it claims reports the catalog it read, and a scan a crash cut short is
read again.

Every pass here runs over the fake bridge and runner (``fakes``): no host,
socket or credential.
"""

import json
import tempfile
import unittest
from pathlib import Path

from hq_runner.claim import CAPABILITIES
from hq_runner.journal import Journal
from hq_runner.worker import LEASE_SECONDS, RENEW_EVERY_SECONDS, work_once

from . import fakes

PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "-----END OPENSSH PRIVATE KEY-----"
)


class PassTestCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.journal = Journal(Path(scratch.name) / "journal.json")
        self.clock = fakes.Clock()

    def work(self, bridge: fakes.Bridge, runner: fakes.Runner, scan=fakes.scanned, secrets=()):
        return work_once(
            bridge,
            controller_id=fakes.WORKER,
            installed=fakes.installed,
            reach=fakes.reach,
            journal=self.journal,
            runner=runner,
            scan=scan,
            clock=self.clock,
            secrets=secrets,
        )

    def only_report(self, bridge: fakes.Bridge) -> dict:
        self.assertEqual(len(bridge.reports), 1, bridge.calls)
        return bridge.reports[0]


class SuccessTests(PassTestCase):
    def test_a_run_that_ends_well_reports_success_with_each_task_outcome(self):
        bridge = fakes.Bridge(fakes.pending())
        runner = fakes.Runner(fakes.WELL)

        outcome = self.work(bridge, runner)

        report = self.only_report(bridge)
        self.assertEqual(outcome.operation, fakes.OPERATION)
        self.assertTrue(report["success"])
        self.assertEqual(report["observed_generation"], 2)
        self.assertEqual(
            [(e["host"], e["task"], e["outcome"]) for e in report["events"]],
            [
                ("localhost", "Say hello", "ok"),
                ("localhost", "Touch a file", "changed"),
                ("localhost", "Hidden", "ok"),
                ("localhost", "Skip me", "skipped"),
            ],
        )
        self.assertNotIn("status", report)
        self.assertIsNone(self.journal.read())

    def test_ansibles_own_check_of_a_roles_arguments_is_reported_only_when_it_fails(self):
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner(fakes.CHECKED, status="failed"))

        passed, task, failed = (event["event_data"] for event in fakes.CHECKED)
        self.assertEqual(
            [(e["task"], e["outcome"]) for e in self.only_report(bridge)["events"]],
            [(task["task"], "ok"), (failed["task"], "failed")],
        )
        self.assertEqual(passed["task"], failed["task"])

    def test_what_runs_is_the_installed_procedure_on_the_claimed_hosts_with_its_parameters(self):
        bridge = fakes.Bridge(fakes.pending(parameters={"service": "db"}))
        runner = fakes.Runner()

        self.work(bridge, runner)

        (run,) = runner.runs
        self.assertEqual(run.procedure, fakes.PROCEDURES[fakes.PROCEDURE])
        self.assertEqual(run.hosts, (fakes.APP,))
        self.assertEqual(run.parameters, {"service": "db"})
        action, sent = bridge.calls[0]
        self.assertEqual(action, "claim")
        self.assertEqual(sent["capabilities"], CAPABILITIES)
        self.assertIn("automation.operation:run", sent["capabilities"])

    def test_nothing_waiting_runs_nothing_and_reports_nothing(self):
        bridge = fakes.Bridge()
        runner = fakes.Runner()

        self.assertIsNone(self.work(bridge, runner))

        self.assertEqual(runner.runs, [])
        self.assertEqual(bridge.reports, [])


class ResultTests(PassTestCase):
    """A run that succeeded says what its playbook found or did, never only that it ended."""

    def test_the_result_a_playbook_states_with_set_stats_is_its_report_s_message(self):
        bridge = fakes.Bridge(fakes.pending(verification=["The service answers"]))

        self.work(bridge, fakes.Runner([*fakes.WELL[:-1], fakes.STATED]))

        report = self.only_report(bridge)
        self.assertTrue(report["success"])
        self.assertEqual(report["message"], fakes.STATED["event_data"]["artifact_data"]["result"])

    def test_a_result_set_machine_by_machine_names_each_machine(self):
        stated = json.loads(json.dumps(fakes.STATED))
        stated["event_data"]["artifact_data"]["result"] = {"example-b": "Two", "example-a": "One"}
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner([stated]))

        self.assertEqual(self.only_report(bridge)["message"], "example-a: One; example-b: Two")

    def test_a_playbook_that_states_none_reports_what_its_record_says_shows_it_worked(self):
        bridge = fakes.Bridge(fakes.pending(verification=["The service answers", "Its port is open."]))

        self.work(bridge, fakes.Runner(fakes.WELL))

        self.assertEqual(self.only_report(bridge)["message"], "The service answers. Its port is open.")

    def test_a_secret_in_a_stated_result_never_reaches_the_report(self):
        stated = json.loads(json.dumps(fakes.STATED))
        stated["event_data"]["artifact_data"]["result"] = f"Read the key {PRIVATE_KEY}"
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner([stated]))

        self.assertNotIn("PRIVATE KEY", self.only_report(bridge)["message"])


class StatedTests(PassTestCase):
    """A run's report carries the readings and preview its playbook stated
    (``fakes.READINGS``, recorded on two machines and one planted beside them),
    about its own machines alone, each part whole or left out and said."""

    def stating(self, artifact=None, *, parameters=None):
        event = json.loads(json.dumps(fakes.READINGS))
        if artifact is not None:
            event["event_data"]["artifact_data"] = artifact
        bridge = fakes.Bridge(fakes.pending(parameters=parameters))
        self.work(bridge, fakes.Runner([*fakes.WELL[:-1], event]))
        return self.only_report(bridge)

    def test_the_readings_and_preview_a_playbook_stated_on_the_run_s_machine_are_reported(self):
        recorded = fakes.READINGS["event_data"]["artifact_data"]

        report = self.stating()

        self.assertTrue(report["success"])
        self.assertEqual(
            report["stated"],
            {
                "readings": {fakes.APP: recorded["readings"][fakes.APP]},
                "preview": {fakes.APP: recorded["preview"][fakes.APP]},
            },
        )
        self.assertEqual(sorted(report["stated"]["readings"][fakes.APP]), ["host.filesystem", "host.reboot"])

    def test_what_it_stated_about_a_machine_the_run_is_not_on_is_left_out_and_said(self):
        report = self.stating()

        stated_on = {machine for stat in report["stated"].values() for machine in stat}
        self.assertEqual(stated_on, {fakes.APP})
        self.assertNotIn("elsewhere.example.com", json.dumps(report))
        self.assertNotIn("db.example.com", json.dumps(report["stated"]))
        # db.example.com's readings and preview, and elsewhere.example.com's readings.
        self.assertIn("3 parts", report["message"])
        self.assertIn("for a machine it was not run on", report["message"])

    def test_a_stat_of_no_name_a_report_carries_is_dropped(self):
        report = self.stating()

        self.assertEqual(set(report["stated"]), {"readings", "preview"})
        self.assertNotIn("not a stat HQ takes", json.dumps(report))

    def test_a_part_past_a_bound_is_left_out_whole_and_the_rest_reported(self):
        record = {"mount": "/", "free_bytes": 1, "total_bytes": 2, "read_at": "2026-11-13T12:00:00Z"}
        long = "x" * 501
        cases = {
            "more records of one kind than the bound": [record] * 201,
            "a value longer than a text": [{**record, "mount": long}],
            "a nested value longer than a text": [{"packages": [long]}],
            "a member name longer than a text": [{long: 1}],
        }
        for case, records in cases.items():
            with self.subTest(case):
                report = self.stating(
                    {"readings": {fakes.APP: {"host.filesystem": records, "host.reboot": [{"required": True}]}}}
                )

                self.assertEqual(report["stated"], {"readings": {fakes.APP: {"host.reboot": [{"required": True}]}}})
                self.assertIn(
                    "1 part of what the playbook stated is not reported: 1 past the bridge's bounds", report["message"]
                )

    def test_a_kind_named_past_its_bound_is_left_out(self):
        report = self.stating({"readings": {fakes.APP: {"host." + "x" * 96: [{"mount": "/"}]}}})

        self.assertNotIn("stated", report)
        self.assertIn("past the bridge's bounds", report["message"])

    def test_a_preview_past_a_bound_is_left_out_whole(self):
        for case, lines in {
            "more lines than the bound": ["line"] * 201,
            "a line longer than a text": ["x" * 501],
        }.items():
            with self.subTest(case):
                report = self.stating({"preview": {fakes.APP: lines}})

                self.assertNotIn("stated", report)
                self.assertIn("past the bridge's bounds", report["message"])

    def test_everything_stated_past_the_bound_in_all_is_left_out(self):
        from hq_runner.events import STATED_BYTES

        kinds = {f"host.part{index}": [{"text": "x" * 400} for _ in range(10)] for index in range(20)}

        report = self.stating({"readings": {fakes.APP: kinds}, "preview": {fakes.APP: ["line"]}})

        self.assertGreater(len(json.dumps(kinds)), STATED_BYTES)
        self.assertNotIn("stated", report)
        self.assertIn(
            "21 parts of what the playbook stated are not reported: 21 past the bridge's bounds", report["message"]
        )

    def test_a_part_that_is_neither_a_reading_nor_a_preview_is_left_out(self):
        cases = {
            "readings that are not keyed by machine": {"readings": ["host.filesystem"]},
            "a machine's readings that are not keyed by kind": {"readings": {fakes.APP: ["host.filesystem"]}},
            "records that are not a list": {"readings": {fakes.APP: {"host.filesystem": {"mount": "/"}}}},
            "a record that is not an object": {"readings": {fakes.APP: {"host.filesystem": ["/"]}}},
            "a preview line that is not text": {"preview": {fakes.APP: [1]}},
        }
        for case, artifact in cases.items():
            with self.subTest(case):
                report = self.stating(artifact)

                self.assertNotIn("stated", report)
                self.assertIn("not shaped as a reading or a preview", report["message"])

    def test_a_secret_in_what_a_playbook_stated_never_reaches_the_report(self):
        report = self.stating(
            {
                "readings": {fakes.APP: {"host.filesystem": [{"mount": "/", "note": f"key {PRIVATE_KEY}"}]}},
                "preview": {fakes.APP: ["would write example-learned-secret to /etc/example.conf"]},
            },
            parameters={"api_token": "example-learned-secret"},
        )

        said = json.dumps(report["stated"])
        self.assertNotIn("PRIVATE KEY", said)
        self.assertNotIn("example-learned-secret", said)
        self.assertIn("[redacted]", said)


class FailedRunTests(PassTestCase):
    def test_a_failed_task_reports_failure_and_names_the_task(self):
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner(fakes.RECORDED, status="failed"))

        report = self.only_report(bridge)
        self.assertFalse(report["success"])
        self.assertIn(("localhost", "Fail", "failed"), [(e["host"], e["task"], e["outcome"]) for e in report["events"]])
        self.assertEqual(report["conditions"][0]["type"], "Degraded")


class StoppedRunTests(PassTestCase):
    """A run Ansible stops before its first task says the error it stopped
    with, from the runs ``fakes.STOPPED`` recorded."""

    def test_a_run_stopped_before_its_first_task_says_ansible_s_error_and_where(self):
        cases = {
            "broken_role": ("YAML parsing failed", "roles/broken/tasks/main.yml:4:1"),
            "missing_collection": ("example.absent.compose_restart", "playbooks/missing_collection.yml:7:7"),
        }
        for name, (said, where) in cases.items():
            with self.subTest(name):
                bridge = fakes.Bridge(fakes.pending())

                self.work(bridge, fakes.Runner(fakes.STOPPED[name], status="failed"))

                report = bridge.reports[-1]
                self.assertFalse(report["success"])
                self.assertEqual(report["events"], [])
                self.assertIn(said, report["message"])
                self.assertIn(where, report["message"])
                self.assertNotIn("/srv/example/project", report["message"])
                self.assertNotIn("\x1b", report["message"])
                self.assertNotIn("[ERROR]", report["message"])
                self.assertEqual(report["conditions"][0]["message"], report["message"])

    def test_a_secret_parameter_in_ansible_s_error_is_struck(self):
        bridge = fakes.Bridge(fakes.pending(parameters={"api_token": "example.absent.compose_restart"}))

        self.work(bridge, fakes.Runner(fakes.STOPPED["missing_collection"], status="failed"))

        message = bridge.reports[-1]["message"]
        self.assertNotIn("example.absent.compose_restart", message)
        self.assertIn("[redacted]", message)

    def test_an_error_line_never_replaces_a_failed_task(self):
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner([*fakes.STOPPED["broken_role"], *fakes.RECORDED], status="failed"))

        report = bridge.reports[-1]
        self.assertNotIn("YAML parsing failed", report["message"])
        self.assertIn("failed", [event["outcome"] for event in report["events"]])

    def test_a_place_outside_the_project_is_never_said(self):
        moved = json.loads(json.dumps(fakes.STOPPED["missing_collection"]))
        for event in moved:
            event["stdout"] = event["stdout"].replace("/srv/example/project", "/srv/example/elsewhere")
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner(moved, status="failed"))

        message = bridge.reports[-1]["message"]
        self.assertIn("example.absent.compose_restart", message)
        self.assertNotIn("elsewhere", message)


class TimeoutTests(PassTestCase):
    def test_a_run_ansible_runner_ended_at_its_timeout_reports_timed_out(self):
        bridge = fakes.Bridge(fakes.pending(timeout=30))
        runner = fakes.Runner(fakes.OUTCOMES[:2], status="timeout")

        self.work(bridge, runner)

        (run,) = runner.runs
        self.assertEqual(run.timeout_seconds, 30)
        report = self.only_report(bridge)
        self.assertFalse(report["success"])
        self.assertEqual(report["conditions"][0]["reason"], "TimedOut")
        self.assertIn("30 second", report["message"])
        self.assertEqual(len(report["events"]), 2)


class LeaseTests(PassTestCase):
    def test_a_long_run_renews_its_lease_as_it_goes(self):
        bridge = fakes.Bridge(fakes.pending(timeout=3600))

        def later():
            self.clock.now += RENEW_EVERY_SECONDS

        self.work(bridge, fakes.Runner(fakes.OUTCOMES[:4], between=later))

        renewals = [call for call in bridge.calls if call[0] == "renew"]
        # One per renewal interval that passed.
        self.assertEqual(len(renewals), 4)
        self.assertTrue(all(call[1]["operation"] == fakes.OPERATION for call in renewals))
        self.assertLess(RENEW_EVERY_SECONDS * 2, LEASE_SECONDS)
        self.assertTrue(self.only_report(bridge)["success"])

    def test_a_pause_answered_to_a_renewal_stops_the_run_and_it_reports(self):
        held = {"ok": True, "continue": False, "reason": "Held", "lease_expires_at": "2026-10-03T12:04:00+00:00"}
        bridge = fakes.Bridge(fakes.pending(), renewals=[held])

        def later():
            self.clock.now += RENEW_EVERY_SECONDS

        runner = fakes.Runner(fakes.OUTCOMES, between=later)

        self.work(bridge, runner)

        report = self.only_report(bridge)
        self.assertFalse(report["success"])
        self.assertEqual(report["conditions"][0]["reason"], "Held")
        self.assertEqual(len(report["events"]), 1)

    def test_a_cancelled_run_stops_at_its_next_renewal(self):
        cancelled = {"ok": True, "continue": False, "reason": "Cancelled", "lease_expires_at": None}
        bridge = fakes.Bridge(fakes.pending(), renewals=[cancelled])

        def later():
            self.clock.now += RENEW_EVERY_SECONDS

        self.work(bridge, fakes.Runner(fakes.OUTCOMES, between=later))

        self.assertEqual(self.only_report(bridge)["conditions"][0]["reason"], "Cancelled")


class InvalidClaimTests(PassTestCase):
    def test_a_run_reviewed_at_another_revision_than_installed_is_refused_before_it_runs(self):
        bridge = fakes.Bridge(fakes.pending(revision=fakes.OTHER_REVISION))
        runner = fakes.Runner(fakes.RECORDED)

        self.work(bridge, runner)

        self.assertEqual(runner.runs, [])
        report = self.only_report(bridge)
        self.assertFalse(report["success"])
        self.assertEqual(report["conditions"][0]["reason"], "RevisionMismatch")
        self.assertEqual(report["events"], [])

    def test_a_claim_without_its_run_input_is_refused_before_it_runs(self):
        answer = fakes.pending()
        del answer["input"]
        bridge = fakes.Bridge(answer)
        runner = fakes.Runner()

        self.work(bridge, runner)

        self.assertEqual(runner.runs, [])
        self.assertEqual(self.only_report(bridge)["conditions"][0]["reason"], "Unrunnable")

    def test_a_procedure_not_installed_is_refused_before_it_runs(self):
        answer = fakes.pending()
        answer["input"]["operation"] = answer["resource"]["spec"]["operation"] = "example.other"
        bridge = fakes.Bridge(answer)
        runner = fakes.Runner()

        self.work(bridge, runner)

        self.assertEqual(runner.runs, [])
        self.assertEqual(self.only_report(bridge)["conditions"][0]["reason"], "NotInstalled")


class LoginTests(PassTestCase):
    """Each machine is reached through the login its procedure's privilege
    names, a procedure naming none through the read login, which never
    escalates."""

    def logins(self, **asked) -> tuple[list, list[tuple[str, str]], dict]:
        looked: list[tuple[str, str]] = []

        def reach(host: str, profile: str):
            looked.append((host, profile))
            return fakes.reach(host, fakes.PRIVILEGE)

        bridge = fakes.Bridge(fakes.pending(**asked))
        runner = fakes.Runner()
        work_once(
            bridge,
            controller_id=fakes.WORKER,
            installed=fakes.installed,
            reach=reach,
            journal=self.journal,
            runner=runner,
            scan=fakes.scanned,
            clock=self.clock,
        )
        return runner.runs, looked, self.only_report(bridge)

    def test_a_change_goes_through_its_privilege_s_login_and_may_escalate(self):
        (run,), looked, _ = self.logins()

        self.assertEqual(looked, [(fakes.APP, fakes.PRIVILEGE)])
        self.assertTrue(run.escalates)

    def test_a_read_goes_through_the_read_login_and_never_escalates(self):
        for asked in ({"effect": "read", "privilege": ""}, {"effect": "infrastructure_change", "privilege": ""}):
            with self.subTest(**asked):
                (run,), looked, _ = self.logins(**asked)

                self.assertEqual(looked, [(fakes.APP, "read")])
                self.assertFalse(run.escalates)

    def test_a_machine_no_login_reaches_as_the_profile_is_refused_naming_it(self):
        bridge = fakes.Bridge(fakes.pending(effect="read", privilege=""))
        runner = fakes.Runner()

        self.work(bridge, runner)

        self.assertEqual(runner.runs, [])
        report = self.only_report(bridge)
        self.assertEqual(report["conditions"][0]["reason"], "NoConnection")
        self.assertIn("read", report["message"])

    def test_a_record_that_is_a_read_naming_a_privilege_is_refused(self):
        runs, looked, report = self.logins(effect="read")

        self.assertEqual((runs, looked), ([], []))
        self.assertEqual(report["conditions"][0]["reason"], "Unrunnable")


class RedactionTests(PassTestCase):
    def test_a_secret_in_an_event_never_reaches_the_report(self):
        api_key = "example-api-key-0123456789"
        leaky = fakes.recorded("Fail", "runner_on_failed")
        leaky["event_data"]["res"]["msg"] = f"could not log in with {api_key}: {PRIVATE_KEY}"
        hello = fakes.recorded("Say hello", "runner_on_ok")
        hello["event_data"]["res"]["msg"] = "connected with password=correct-horse-battery"
        hello["event_data"]["res"]["stdout"] = api_key
        bridge = fakes.Bridge(fakes.pending(parameters={"service": "web", "api_key": api_key}))

        self.work(bridge, fakes.Runner([hello, leaky], status="failed"))

        sent = json.dumps(self.only_report(bridge))
        for secret in (api_key, "correct-horse-battery", "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ", "PRIVATE KEY"):
            self.assertNotIn(secret, sent)
        self.assertIn("[redacted]", sent)

    def test_a_task_marked_no_log_reports_only_that_it_was_hidden(self):
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner([fakes.recorded("Hidden", "runner_on_ok")]))

        (event,) = self.only_report(bridge)["events"]
        self.assertNotIn("censored", event["message"])
        self.assertIn("no_log", event["message"])

    def test_every_event_is_held_to_the_report_s_bounds(self):
        long = fakes.recorded("Say hello", "runner_on_ok")
        long["event_data"]["res"]["msg"] = "x" * 5000
        long["event_data"]["task"] = "t" * 5000
        fine = [long] * 250
        broken = fakes.recorded("Fail", "runner_on_failed")
        bridge = fakes.Bridge(fakes.pending(timeout=3600))

        self.work(bridge, fakes.Runner([*fine, broken], status="failed"))

        report = self.only_report(bridge)
        self.assertLessEqual(len(report["events"]), 200)
        self.assertEqual(report["events"][-1]["outcome"], "failed")
        self.assertTrue(all(len(e["message"]) <= 500 and len(e["task"]) <= 200 for e in report["events"]))


class RestartTests(PassTestCase):
    def test_a_run_reclaimed_after_a_crash_reports_its_outcome_unknown_and_never_runs_again(self):
        crashed = fakes.Bridge(fakes.pending())

        def crash():
            raise SystemExit("the worker stopped")

        with self.assertRaises(SystemExit):
            self.work(crashed, fakes.Runner(fakes.RECORDED, between=crash))
        attempt = crashed.calls[0][1]["attempt"]
        # The lease is still live, so the retried claim is answered the same run.
        bridge = fakes.Bridge(fakes.pending())
        runner = fakes.Runner(fakes.RECORDED)

        self.work(bridge, runner)

        self.assertEqual(bridge.calls[0][1]["attempt"], attempt)
        self.assertEqual(runner.runs, [])
        report = self.only_report(bridge)
        self.assertFalse(report["success"])
        self.assertEqual(report["conditions"][0]["reason"], "OutcomeUnknown")
        self.assertIsNone(self.journal.read())

    def test_a_claim_whose_answer_was_lost_runs_once_under_the_same_attempt(self):
        lost = fakes.Bridge(fakes.pending())

        def unanswered(*_args, **_kwargs):
            raise ConnectionError("the claim's answer was lost")

        lost.claim = unanswered
        with self.assertRaises(ConnectionError):
            self.work(lost, fakes.Runner())
        attempt = self.journal.read().attempt
        bridge = fakes.Bridge(fakes.pending())
        runner = fakes.Runner(fakes.WELL)

        self.work(bridge, runner)

        self.assertEqual(bridge.calls[0][1]["attempt"], attempt)
        self.assertEqual(len(runner.runs), 1)
        self.assertTrue(self.only_report(bridge)["success"])

    def test_a_report_that_was_not_delivered_is_sent_again_as_written(self):
        first = fakes.Bridge(fakes.pending())
        first.fail_report = True
        from hq_runner.bridge import BridgeError

        with self.assertRaises(BridgeError):
            self.work(first, fakes.Runner(fakes.WELL))
        bridge = fakes.Bridge(fakes.pending())
        runner = fakes.Runner(fakes.RECORDED)

        self.work(bridge, runner)

        self.assertEqual(runner.runs, [])
        self.assertTrue(self.only_report(bridge)["success"])


class ScanTests(PassTestCase):
    CATALOG = {
        "branch": "main",
        "revision": fakes.REVISION,
        "verification": {"verified": True, "reason": "valid", "committer": "web-flow"},
        "procedures": [],
        "files": [],
        "unpinned": [],
        "reaches": [],
    }

    def test_a_claimed_scan_reports_its_catalog_under_its_record_s_generation_and_runs_nothing(self):
        bridge = fakes.Bridge(fakes.scan_pending(generation=3))
        runner = fakes.Runner(fakes.RECORDED)

        outcome = self.work(bridge, runner, scan=lambda: self.CATALOG)

        self.assertEqual(outcome.operation, fakes.SCAN_OPERATION)
        report = self.only_report(bridge)
        self.assertTrue(report["success"])
        self.assertEqual(report["observed_generation"], 3)
        self.assertEqual(report["catalog"], self.CATALOG)
        self.assertEqual(report["events"], [])
        self.assertEqual(runner.runs, [])
        self.assertIsNone(self.journal.read())

    def test_a_scan_that_cannot_read_its_source_reports_why_and_no_catalog(self):
        from hq_runner.claim import Refused

        def refused():
            raise Refused("FetchFailed", "git fetch failed: ghs_example0123456789token was refused")

        bridge = fakes.Bridge(fakes.scan_pending())

        self.work(bridge, fakes.Runner(), scan=refused, secrets=("ghs_example0123456789token",))

        report = self.only_report(bridge)
        self.assertFalse(report["success"])
        self.assertNotIn("catalog", report)
        self.assertEqual(report["conditions"][0]["reason"], "FetchFailed")
        self.assertNotIn("ghs_example0123456789token", json.dumps(report))

    def test_the_token_never_reaches_a_catalog(self):
        token = "ghs_example0123456789token"
        leaked = {**self.CATALOG, "files": [{"path": f"notes/{token}.txt", "object": fakes.REVISION}]}
        bridge = fakes.Bridge(fakes.scan_pending())

        self.work(bridge, fakes.Runner(), scan=lambda: leaked, secrets=(token,))

        self.assertNotIn(token, json.dumps(self.only_report(bridge)))

    def test_a_scan_a_crash_cut_short_is_read_again_rather_than_reported_unknown(self):
        def crash():
            raise SystemExit("the worker stopped")

        with self.assertRaises(SystemExit):
            self.work(fakes.Bridge(fakes.scan_pending()), fakes.Runner(), scan=crash)
        bridge = fakes.Bridge(fakes.scan_pending())

        self.work(bridge, fakes.Runner(), scan=lambda: self.CATALOG)

        report = self.only_report(bridge)
        self.assertTrue(report["success"])
        self.assertEqual(report["catalog"], self.CATALOG)
