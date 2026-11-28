"""The release production ran before a release, and what it runs now, from GitHub's deployments."""

import json

from django.test import SimpleTestCase, tag

from tools.pipeline import production

RELEASE = "e" * 40
BEFORE = "c" * 40
OLDER = "b" * 40
NEWER = "f" * 40


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


# Each reads the outcomes the Deploy workflow writes, deploy/outcomes.json.
@tag("source")
class PreviousReleaseTests(SimpleTestCase):
    def test_a_live_release_is_compared_with_the_one_live_before_it(self):
        found = production.releases(RELEASE, DEPLOYMENTS, STATUSES.__getitem__)
        self.assertEqual((found.previous, found.deployed, found.running), (BEFORE, True, RELEASE))

    def test_a_release_never_live_is_compared_with_what_production_runs(self):
        found = production.releases(RELEASE, DEPLOYMENTS[1:], STATUSES.__getitem__)
        self.assertEqual((found.previous, found.deployed, found.running), (BEFORE, False, BEFORE))

    def test_no_earlier_live_release_leaves_nothing_to_compare(self):
        found = production.releases(RELEASE, DEPLOYMENTS[:2], STATUSES.__getitem__)
        self.assertIsNone(found.previous)


@tag("source")
class OutcomeTests(SimpleTestCase):
    """A release whose deployment says more than live or not."""

    def setUp(self):
        self.outcomes = json.loads(production.OUTCOMES.read_text())["outcomes"]

    def reported(self, name):
        outcome = self.outcomes[name]
        return status(outcome["state"], outcome["description"])

    def found(self, release, deployments, statuses):
        return production.releases(release, deployments, {**STATUSES, **statuses}.__getitem__)

    def test_a_release_live_with_a_check_failed_does_not_read_as_one_that_passed(self):
        clean = self.found(RELEASE, DEPLOYMENTS, {5: [self.reported("live"), status("in_progress")]})
        checked = self.found(RELEASE, DEPLOYMENTS, {5: [self.reported("live-check-failed"), status("in_progress")]})

        self.assertEqual((checked.deployed, checked.running), (True, RELEASE))
        self.assertFalse(clean.check_failed)
        self.assertTrue(checked.check_failed)

    def test_a_deploy_that_leaves_production_unknown_says_so(self):
        # A rollback that failed, and a deploy interrupted after hq-host began.
        unknown = [name for name, outcome in self.outcomes.items() if outcome["production"] == "unknown"]
        self.assertIn("rollback-failed", unknown)
        newer = [deployment(6, NEWER, "2026-10-31T09:00:00Z"), *DEPLOYMENTS]
        for name in unknown:
            with self.subTest(outcome=name):
                found = self.found(NEWER, newer, {6: [self.reported(name), status("in_progress")]})

                self.assertIsNone(found.running)
                self.assertEqual(found.unknown_since, NEWER)
                # The release live before it is still the one to compare with.
                self.assertEqual(found.previous, RELEASE)
