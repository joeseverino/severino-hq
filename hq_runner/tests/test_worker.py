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


class FailedRunTests(PassTestCase):
    def test_a_failed_task_reports_failure_and_names_the_task(self):
        bridge = fakes.Bridge(fakes.pending())

        self.work(bridge, fakes.Runner(fakes.RECORDED, status="failed"))

        report = self.only_report(bridge)
        self.assertFalse(report["success"])
        self.assertIn(("localhost", "Fail", "failed"), [(e["host"], e["task"], e["outcome"]) for e in report["events"]])
        self.assertEqual(report["conditions"][0]["type"], "Degraded")


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
