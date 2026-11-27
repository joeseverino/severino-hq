from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import PULL_REQUEST_KIND, REPOSITORY_KIND, WORKFLOW_RUN_KIND

from ..attention import raised
from ..dashboard import work_queue
from ..findings import finding_key
from ..freshness import stale_after
from ..github_estate import Repository, workflow_run
from ..repository_findings import PROJECTS


def raised_items():
    """What Projects raises on the queue: its repository rules' findings."""

    return tuple(raised(PROJECTS))


def store(pull_requests=(), runs=(), **record):
    """One repository's reading, ``example/alpha`` unless ``record`` names
    another, with ``pull_requests`` and ``runs`` the readings of its pass."""

    named = {"connection_ref": "github", "repository": record.get("repository", "example/alpha")}
    held = {
        REPOSITORY_KIND: [
            {
                **named,
                "url": "https://github.com/example/alpha",
                "default_branch": "main",
                "head": {"sha": "abc"},
                **record,
            }
        ],
        PULL_REQUEST_KIND: [named | pull for pull in pull_requests],
        WORKFLOW_RUN_KIND: [named | run for run in runs],
    }
    for kind, records in held.items():
        ProviderInventory.objects.update_or_create(
            kind=kind,
            defaults={
                "records": records,
                "reachable": True,
                "connected": True,
                "observed_at": timezone.now(),
                "refused_parts": [{"part": "dependabot", "scope": named["repository"]}]
                if kind == REPOSITORY_KIND
                else [],
            },
        )


def waiting_deploy() -> None:
    started = (timezone.now() - timedelta(hours=3)).isoformat()
    store(waiting=[{"id": 9, "name": "Deploy", "environments": ["production"], "created_at": started, "url": "u"}])


def read(ago: timedelta) -> None:
    ProviderInventory.objects.filter(kind=REPOSITORY_KIND).update(observed_at=timezone.now() - ago)


class ReadingAgeTests(TestCase):
    """An item says how old the reading behind it is, and an old reading
    never stands in the queue as something happening now."""

    def test_an_item_from_an_out_of_date_reading_is_a_notice_of_what_was_read(self):
        waiting_deploy()
        read(stale_after(REPOSITORY_KIND) + timedelta(hours=1))

        (item,) = raised_items()
        queued = [entry for entry in work_queue() if entry["key"].endswith(finding_key(item))]

        self.assertTrue(item.notice)
        self.assertNotEqual(item.severity, "serious")
        self.assertIsNotNone(item.observed_at)
        # Still in the queue, as a notice: an old reading does not vanish.
        self.assertEqual([(entry["notice"], bool(entry["observed_at"])) for entry in queued], [(True, True)])


# A record as the controller writes it: one update, one pull request a person
# opened, and Dependabot alerts, one of which the update answers.
UPDATE = {
    "number": 6,
    "title": "Update example-lib to v2",
    "url": "https://github.com/example/alpha/pull/6",
    "author": "renovate[bot]",
    "draft": False,
    "created_at": "2030-01-03T00:00:00Z",
    "updated_at": "2030-01-04T00:00:00Z",
    "head": "eeee",
    "branch": "renovate/example-lib-2.x",
    "labels": ["dependencies", "security"],
    "checks": {"state": "failure", "total": 2, "failing": ["test"], "running": 0, "names": ["build", "test"]},
}
BUMP = {
    "number": 5,
    "title": "Bump",
    "url": "https://github.com/example/alpha/pull/5",
    "author": "octo",
    "draft": True,
    "created_at": "2030-01-01T00:00:00Z",
    "updated_at": "2030-01-02T00:00:00Z",
    "head": "bbbb",
    "branch": "bump",
    "labels": [],
    "checks": None,
}
ANSWERED = {
    "number": 3,
    "package": "Example-Lib",
    "ecosystem": "npm",
    "manifest_path": "web/package.json",
    "severity": "critical",
    "first_patched_version": "2.0.1",
    "url": "https://github.com/example/alpha/security/dependabot/3",
}
UNANSWERED = {
    "number": 2,
    "package": "example.com/unfixed",
    "ecosystem": "go",
    "manifest_path": "go.mod",
    "severity": "high",
    "first_patched_version": None,
    "url": "https://github.com/example/alpha/security/dependabot/2",
}


def repository(pull_requests=(), **record) -> Repository:
    return Repository(
        "example/alpha", {"repository": "example/alpha", **record}, None, pull_requests=tuple(pull_requests)
    )


class LeakedCredentialItemTests(TestCase):
    def test_a_leaked_credential_beside_a_high_alert_is_one_item_linking_each_kind(self):
        store(alerts={"leaked_credentials": {"leaked": 1}, "dependabot": {"high": 1}})

        (item,) = raised_items()

        self.assertEqual(item.evidence, (("Open", "2"),))
        self.assertEqual(
            {link.url for link in item.pages}, {f"{ALPHA}/security/dependabot", f"{ALPHA}/security/secret-scanning"}
        )


ALPHA = "https://github.com/example/alpha"


class WorkflowRunTests(SimpleTestCase):
    def test_a_finished_run_with_no_verdict_is_neutral(self):
        self.assertEqual(workflow_run({"status": "completed", "conclusion": "skipped"}).status, "neutral")


class ToneTests(SimpleTestCase):
    def test_alerts_are_serious_by_a_serious_one_attention_by_any_good_when_clear_and_toneless_unoffered(self):
        def tone(**counts):
            return repository(alerts={"dependabot": counts}).alerts_tone

        self.assertEqual((tone(high=1, low=2), tone(low=1), tone(low=0)), ("serious", "attention", "good"))
        self.assertEqual(repository().alerts_tone, "")
