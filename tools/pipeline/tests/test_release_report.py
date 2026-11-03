"""The release report compares with the release production ran before, and a failed reading is a row."""

import json
from pathlib import Path

from django.test import SimpleTestCase

from tools.pipeline import release_report

ROOT = Path(__file__).resolve().parents[3]
RELEASE = "e" * 40
BEFORE = "c" * 40
OLDER = "b" * 40
NEWER = "f" * 40


def declared():
    """deploy/outcomes.json's outcomes, by name: what the Deploy workflow writes on a deployment."""
    return json.loads((ROOT / "deploy" / "outcomes.json").read_text())["outcomes"]


def deployment(ident, sha, created_at):
    """One entry of `GET /repos/{owner}/{repo}/deployments`, as the REST API returns it."""
    return {
        "id": ident,
        "sha": sha,
        "ref": sha,
        "task": "deploy",
        "environment": "production",
        "production_environment": True,
        "created_at": created_at,
        "payload": {"image": "ghcr.example.com/hq@sha256:" + "0" * 64},
    }


def status(state, description="Live: passed its health check"):
    """One entry of `GET .../deployments/{id}/statuses`."""
    return {
        "state": state,
        "description": description,
        "environment": "production",
        "created_at": "2026-10-25T00:40:17Z",
    }


# Newest first, as the API lists them: the release's own run failed once before it went live.
DEPLOYMENTS = [
    deployment(5, RELEASE, "2026-10-31T05:00:00Z"),
    deployment(4, RELEASE, "2026-10-31T02:19:12Z"),
    deployment(3, BEFORE, "2026-10-25T00:36:00Z"),
    deployment(2, OLDER, "2026-10-22T17:16:35Z"),
]
STATUSES = {
    5: [status("success"), status("in_progress"), status("queued")],
    4: [status("failure", "Not deployed"), status("in_progress"), status("queued")],
    # Marked inactive once a newer release went live: it was live, all the same.
    3: [status("inactive"), status("success"), status("queued")],
    2: [status("inactive"), status("success"), status("queued")],
}


def side(commit, idle, server_ms):
    return {
        "commit": commit[:12],
        "pages": [
            {
                "page": "/",
                "server": {"p50_ms": server_ms, "p95_ms": server_ms, "queries": 3, "rows": 1, "html_bytes": 2048},
            }
        ],
        "memory": {"idle_bytes": idle, "after_pass_bytes": idle * 2},
        "cold_start": {"median_ms": 1300.0},
        "failures": [],
    }


class PreviousReleaseTests(SimpleTestCase):
    def test_a_live_release_is_compared_with_the_one_live_before_it(self):
        found = release_report.releases(RELEASE, DEPLOYMENTS, STATUSES.__getitem__)
        self.assertEqual((found.previous, found.deployed, found.running), (BEFORE, True, RELEASE))

    def test_a_release_never_live_is_compared_with_what_production_runs(self):
        found = release_report.releases(RELEASE, DEPLOYMENTS[1:], STATUSES.__getitem__)
        self.assertEqual((found.previous, found.deployed, found.running), (BEFORE, False, BEFORE))

    def test_no_earlier_live_release_leaves_nothing_to_compare(self):
        found = release_report.releases(RELEASE, DEPLOYMENTS[:2], STATUSES.__getitem__)
        self.assertIsNone(found.previous)


class OutcomeTests(SimpleTestCase):
    """What the report says of a release whose deployment says more than live or not."""

    def setUp(self):
        self.outcomes = declared()

    def reported(self, name):
        outcome = self.outcomes[name]
        return status(outcome["state"], outcome["description"])

    def said(self, release, statuses):
        found = release_report.releases(release, DEPLOYMENTS, {**STATUSES, **statuses}.__getitem__)
        return found, release_report.verdict(release_report.Report(release, base=BEFORE), found)

    def test_a_release_live_with_a_check_failed_does_not_read_as_one_that_passed(self):
        _, clean = self.said(RELEASE, {5: [self.reported("live"), status("in_progress")]})
        found, checked = self.said(RELEASE, {5: [self.reported("live-check-failed"), status("in_progress")]})

        self.assertTrue(found.deployed)
        self.assertEqual(found.running, RELEASE)
        self.assertNotEqual(clean, checked)

    def test_a_deploy_that_leaves_production_unknown_says_so(self):
        # A rollback that failed, and a deploy interrupted after hq-host began.
        unknown = [name for name, outcome in self.outcomes.items() if outcome["production"] == "unknown"]
        self.assertIn("rollback-failed", unknown)
        newer = [deployment(6, NEWER, "2026-10-31T09:00:00Z"), *DEPLOYMENTS]
        for name in unknown:
            with self.subTest(outcome=name):
                statuses = {**STATUSES, 6: [self.reported(name), status("in_progress")]}

                found = release_report.releases(NEWER, newer, statuses.__getitem__)
                said = release_report.verdict(release_report.Report(NEWER, base=RELEASE), found)

                self.assertIsNone(found.running)
                self.assertNotIn(f"production runs {RELEASE[:12]}", said)
                self.assertIn("unknown", said)
                # The release live before it is still the one to compare with.
                self.assertEqual(found.previous, RELEASE)


class RenderTests(SimpleTestCase):
    def made(self):
        return release_report.Report(RELEASE, base=BEFORE)

    def test_the_headline_sets_each_number_beside_its_move(self):
        made = self.made()
        release_report.perf_rows(
            made, {"run": "r", "here": side(RELEASE, 100 * 1024**2, 30.0), "base": side(BEFORE, 120 * 1024**2, 25.0)}
        )
        rows = {row[0]: row for row in made.perf}
        self.assertEqual(rows["memory idle"][1:], ("120.0 MB", "100.0 MB", "-20.0"))
        self.assertEqual(rows["p50 ms, every page summed"][3], "+5.0")
        self.assertEqual(made.failed, [])
        self.assertIn("moved past their spread", release_report.verdict(made, None))

    def test_a_kept_base_names_its_run_and_the_load_of_each_side(self):
        made = self.made()
        doc = {
            "run": "r",
            "base_reused_from": "kept-run",
            "here": side(RELEASE, 1, 1.0) | {"load": 4.5},
            "base": side(BEFORE, 1, 1.0) | {"load": 5.75},
        }
        release_report.perf_rows(made, doc)
        written = release_report.render(made, None)
        self.assertRegex(written, r"kept from run kept-run\b.*\b5\.75\b.*\b4\.5\b")

    def test_a_base_measured_host_alone_says_so(self):
        made = self.made()
        doc = {
            "run": "r",
            "alone": "the base refused the extension set",
            "here": side(RELEASE, 1, 1.0),
            "base": side(BEFORE, 1, 1.0),
        }
        release_report.perf_rows(made, doc)
        self.assertIn("the base refused the extension set", release_report.render(made, None))

    def test_a_failed_reading_is_a_row_and_counted_in_the_verdict(self):
        made = self.made()
        release_report.production_rows(made, {"error": "RuntimeError: no process runs hq.config.asgi:application"})
        release_report.host_rows(
            made,
            {
                "drift": {"error": "RuntimeError: journalctl exited 1"},
                "failed": {"value": ["severino-hq-x.service"]},
                "units": {"severino-hq-glance.service": {"value": {"ExecMainStartTimestampMonotonic": "0"}}},
            },
        )
        written = release_report.render(made, None)
        self.assertIn("no process runs hq.config.asgi:application", written)
        self.assertIn("no run recorded", written)
        closing = written.strip().splitlines()[-1]
        self.assertIn("2 readings failed", closing)
        self.assertIn("severino-hq-x.service", closing)

    def test_a_release_not_deployed_says_what_production_runs(self):
        found = release_report.Releases(previous=BEFORE, deployed=False, running=BEFORE)
        self.assertIn(f"production runs {BEFORE[:12]}", release_report.verdict(self.made(), found))

    def test_a_unit_run_is_timed_from_its_main_process(self):
        said = release_report.last_run(
            {
                "ExecMainStartTimestamp": "Sat 2026-10-31 04:42:09 UTC",
                "ExecMainStartTimestampMonotonic": "1469026336986",
                "ExecMainExitTimestampMonotonic": "1469027263396",
                "Result": "success",
                "ExecMainStatus": "0",
                "CPUUsageNSec": "[not set]",
            }
        )
        self.assertIn("0.93 s", said)
        self.assertNotIn("CPU", said)
