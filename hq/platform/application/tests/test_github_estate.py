from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.github import PARTS, REPOSITORY_KIND
from hq.platform.core.testing import operator

from ..attention import raised
from ..dashboard import work_queue
from ..derivation_testing import frozen
from ..finding_steps import Run
from ..findings import finding_key
from ..freshness import stale_after
from ..github_estate import ALERT_PAGES, Repository, repository_for, workflow_run
from ..repository_findings import PROJECTS


def raised_items():
    """What Projects raises on the queue: its repository rules' findings."""

    return tuple(raised(PROJECTS))


def store(**record):
    ProviderInventory.objects.update_or_create(
        kind="github.repository",
        defaults={
            "records": [
                {
                    "connection_ref": "github",
                    "repository": "example/alpha",
                    "url": "https://github.com/example/alpha",
                    "default_branch": "main",
                    "head": {"sha": "abc"},
                    **record,
                }
            ],
            "reachable": True,
            "connected": True,
            "observed_at": timezone.now(),
            "refused_parts": [{"part": "dependabot", "scope": "example/alpha"}],
        },
    )


class EstateTests(TestCase):
    def test_a_project_joins_its_repository_by_url(self):
        store()

        found = repository_for("https://github.com/example/alpha")

        self.assertEqual((found.name, found.refused), ("example/alpha", ("dependabot",)))
        self.assertIsNone(repository_for("https://github.com/example/other"))

    def test_a_project_url_cased_unlike_the_apps_name_still_joins(self):
        store()

        self.assertEqual(repository_for("https://github.com/Example/Alpha.git").name, "example/alpha")


def commands(item) -> list[str]:
    return [step.command for step in item.steps if isinstance(step, Run)]


class FailingRunTests(TestCase):
    RUN = "https://github.com/example/alpha/actions/runs/7"

    def failing(self, *runs):
        store(checks={"state": "failure", "failing": ["lint"]}, failing_runs=list(runs))
        (item,) = raised_items()
        return item

    def test_the_item_opens_the_failing_run_whatever_commit_it_ran_on(self):
        item = self.failing({"id": 7, "name": "Scorecard", "conclusion": "failure", "sha": "old", "url": self.RUN})

        self.assertEqual(item.primary.url, self.RUN)
        self.assertEqual(commands(item), [])

    def test_several_failing_runs_are_each_linked(self):
        item = self.failing(
            {"id": 7, "name": "CI", "conclusion": "failure", "sha": "abc", "url": self.RUN},
            {"id": 8, "name": "Release", "conclusion": "timed_out", "sha": "abc", "url": f"{self.RUN}8"},
        )

        self.assertEqual([link.url for link in item.pages], [self.RUN, f"{self.RUN}8"])

    def test_a_run_address_that_is_not_a_web_address_is_never_a_link(self):
        item = self.failing({"id": 7, "name": "CI", "conclusion": "failure", "sha": "abc", "url": "javascript:x"})

        self.assertEqual(item.primary.url, "https://github.com/example/alpha/commit/abc")

    def test_the_failing_job_is_named_as_what_it_is(self):
        item = self.failing()

        self.assertEqual(item.explanation, "Failing job: lint")

    def test_a_check_no_workflow_runs_is_listed_by_the_command_the_item_offers(self):
        item = self.failing()

        (command,) = commands(item)
        self.assertIn("repos/example/alpha/commits/abc/check-runs", command)


class ProjectPushedTests(TestCase):
    def test_a_project_pushed_since_its_last_edit_is_dated_and_ordered_by_the_push(self):
        from django.urls import reverse

        from hq.domains.projects.models import Project

        from ..sections import recent_active_projects

        store(pushed_at=(timezone.now() - timedelta(minutes=5)).isoformat())
        url = "https://github.com/example/alpha"
        project = Project.objects.create(name="Alpha", slug="alpha", status="active", repository_url=url)
        Project.objects.create(name="Beta", slug="beta", status="active")
        Project.objects.filter(slug="beta").update(updated_at=timezone.now() - timedelta(days=1))
        Project.objects.filter(pk=project.pk).update(updated_at=timezone.now() - timedelta(days=30))
        operator(self.client)

        response = self.client.get(reverse("projects:detail", args=[project.slug]))

        self.assertContains(response, "Last active 5")
        self.assertEqual([row["slug"] for row in recent_active_projects()], ["alpha", "beta"])


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
        self.assertIn("was waiting for your approval", item.title)
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
LOW = {**UNANSWERED, "number": 1, "package": "example-low", "severity": "low"}


def repository(**record) -> Repository:
    return Repository("example/alpha", {"repository": "example/alpha", **record}, None)


class UpdateTests(SimpleTestCase):
    def test_an_update_is_a_pull_request_from_a_renovate_branch_with_its_age(self):
        found = repository(pull_requests=[UPDATE, BUMP])
        opened = datetime(2030, 1, 3, tzinfo=UTC)

        with frozen(opened + timedelta(days=4, hours=2)):
            (update,) = found.update_pulls
            oldest = found.oldest_update_days

        self.assertEqual((update["number"], update["opened"], update["age_days"]), (6, opened, 4))
        self.assertEqual(oldest, 4)
        self.assertEqual([pull["number"] for pull in found.other_pulls], [5])

    def test_an_alert_is_unmatched_unless_an_update_names_its_package(self):
        found = repository(pull_requests=[UPDATE, BUMP], dependabot_alerts=[LOW, ANSWERED, UNANSWERED])

        self.assertEqual([alert["number"] for alert in found.unmatched_alerts], [2, 1])

    def test_without_an_update_every_alert_is_unmatched(self):
        found = repository(pull_requests=[BUMP], dependabot_alerts=[ANSWERED])

        self.assertEqual([alert["number"] for alert in found.unmatched_alerts], [3])

    def test_a_record_with_no_rows_or_none_read_has_nothing_unmatched(self):
        for record in ({}, {"dependabot_alerts": []}, {"dependabot_alerts": None}):
            with self.subTest(record=record):
                found = repository(**record)
                self.assertEqual((found.update_pulls, found.unmatched_alerts, found.oldest_update_days), ([], [], None))


class UnmatchedAlertItemTests(TestCase):
    def test_the_alert_item_counts_the_serious_alerts_no_update_answers_and_opens_the_projects_updates(self):
        from hq.domains.projects.models import Project

        Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha")
        store(
            alerts={"dependabot": {"critical": 1, "high": 1, "low": 1}},
            pull_requests=[UPDATE],
            dependabot_alerts=[ANSWERED, UNANSWERED, LOW],
        )

        (item,) = raised_items()

        self.assertIn("1 has no update open", item.explanation)
        self.assertEqual(item.primary.url, "/projects/alpha/#updates")
        self.assertEqual([link.url for link in item.pages], [f"{ALPHA}/security/dependabot"])

    def test_with_every_serious_alert_answered_the_item_opens_that_parts_alerts(self):
        store(alerts={"dependabot": {"critical": 1}}, pull_requests=[UPDATE], dependabot_alerts=[ANSWERED])

        (item,) = raised_items()

        self.assertNotIn("update pull request", item.explanation)
        self.assertEqual((item.primary.url, item.pages), (f"{ALPHA}/security/dependabot", ()))

    def test_alerts_no_version_fixes_yet_leave_the_queue_and_one_a_version_fixes_keeps_them_in_it(self):
        def queued(*alerts):
            store(
                alerts={"dependabot": {alert["severity"]: 1 for alert in alerts}},
                dependabot_alerts=list(alerts),
            )
            return [
                entry["key"] for entry in work_queue() if entry["key"].endswith("finding:github-alerts::example/alpha")
            ]

        # Nobody can act on an alert no release fixes: its project's updates
        # list it, with "no fixed version yet".
        self.assertEqual(queued(UNANSWERED), [])
        self.assertEqual(len(queued(UNANSWERED, ANSWERED)), 1)

    def test_serious_alerts_in_several_parts_link_each_parts_list_the_worst_first(self):
        store(alerts={"dependabot": {"high": 1}, "code_scanning": {"critical": 1}, "leaked_credentials": {"leaked": 1}})

        (item,) = raised_items()

        self.assertEqual(item.primary.url, f"{ALPHA}/security/code-scanning")
        self.assertEqual(
            [link.url for link in item.pages],
            [f"{ALPHA}/security/code-scanning", f"{ALPHA}/security/dependabot", f"{ALPHA}/security/secret-scanning"],
        )


class LeakedCredentialItemTests(TestCase):
    def test_a_leaked_credential_beside_a_high_alert_is_one_item_linking_each_kind(self):
        store(alerts={"leaked_credentials": {"leaked": 1}, "dependabot": {"high": 1}})

        (item,) = raised_items()

        self.assertEqual(item.evidence, (("Open", "2"),))
        self.assertIn("1 is a leaked credential", item.explanation)
        self.assertEqual(
            {link.url for link in item.pages}, {f"{ALPHA}/security/dependabot", f"{ALPHA}/security/secret-scanning"}
        )


ALPHA = "https://github.com/example/alpha"


class AlertPageTests(SimpleTestCase):
    def test_every_part_that_reads_alerts_has_its_list_on_github(self):
        self.assertEqual(set(ALERT_PAGES), {part for part in PARTS if part.label.endswith(" alerts")})

    def test_a_record_holds_alert_counts_only_as_numbers_by_an_alert_part(self):
        reading = OBSERVATIONS[REPOSITORY_KIND]
        base = {"connection_ref": "github", "repository": "example/alpha"}
        refused = (
            {"leaked_credentials": {"leaked": "[redacted]"}},
            {"dependabot": {"high": "2"}},
            {"dependabot": {"high": True}},
            {"dependabot": {"high": -1}},
            {"dependabot": "[redacted]"},
            {"branch_rules": {"high": 1}},
        )
        for alerts in refused:
            with self.subTest(alerts=alerts):
                self.assertEqual(reading.clean([{**base, "alerts": alerts}]), ([], 1))
        # Every part whose alerts have a list on GitHub may hold counts.
        alerts = {part.name: {"high": 1} for part in ALERT_PAGES}
        kept, refused_count = reading.clean([{**base, "alerts": alerts}])
        self.assertEqual(refused_count, 0)
        self.assertEqual(Repository("example/alpha", kept[0], None).open_alerts, len(ALERT_PAGES))

    def test_a_repository_lists_its_alerts_where_the_worst_are_or_its_overview_when_clear(self):
        cases = (
            ({"leaked_credentials": {"leaked": 1}, "dependabot": {"low": 2}}, f"{ALPHA}/security/dependabot"),
            ({"leaked_credentials": {"leaked": 1}, "dependabot": {"low": 0}}, f"{ALPHA}/security/secret-scanning"),
            ({}, f"{ALPHA}/security"),
        )
        for alerts, url in cases:
            with self.subTest(alerts=alerts):
                self.assertEqual(repository(url=ALPHA, alerts=alerts).alerts_url, url)


class WorkflowRunTests(SimpleTestCase):
    def test_a_run_says_what_it_came_to_where_and_how_long_ago(self):
        run = workflow_run(
            {
                "name": "CI",
                "url": "https://github.com/example/alpha/actions/runs/7",
                "conclusion": "timed_out",
                "status": "completed",
                "branch": "main",
                "sha": "abcdef1234",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )

        self.assertEqual((run.title, run.badge, run.status), ("CI", "Timed out", "serious"))
        self.assertTrue(run.external)
        self.assertTrue(run.meta.startswith("main\xa0· abcdef1\xa0· "))

    def test_a_run_still_going_asks_for_attention(self):
        run = workflow_run({"name": "CI", "status": "in_progress"})

        self.assertEqual((run.badge, run.status, run.meta), ("In progress", "attention", ""))

    def test_a_finished_run_with_no_verdict_is_neutral(self):
        self.assertEqual(workflow_run({"status": "completed", "conclusion": "skipped"}).status, "neutral")

    def test_a_run_s_address_is_a_web_address_or_nothing(self):
        self.assertEqual(workflow_run({"url": "javascript:alert(1)", "conclusion": "success"}).url, "")


class ToneTests(SimpleTestCase):
    def test_checks_are_serious_failing_good_passing_attention_running_and_toneless_when_none_run(self):
        tones = {state: repository(checks={"state": state}).checks_tone for state in ("failure", "success", "pending")}

        self.assertEqual(tones, {"failure": "serious", "success": "good", "pending": "attention"})
        self.assertEqual(repository().checks_tone, "")

    def test_production_is_good_deployed_serious_failed_attention_otherwise_and_toneless_undeployed(self):
        def tone(state):
            return repository(deployments=[{"environment": "production", "state": state}]).production_tone

        self.assertEqual(
            [tone(state) for state in ("success", "failure", "error", "in_progress", "inactive")],
            ["good", "serious", "serious", "attention", "attention"],
        )
        self.assertEqual(repository(deployments=[{"environment": "staging", "state": "success"}]).production_tone, "")

    def test_a_live_release_whose_check_failed_needs_attention_never_good(self):
        def tone(state):
            deployed = {"environment": "production", "state": state, "check_failed": True}
            return repository(deployments=[deployed]).production_tone

        self.assertEqual((tone("success"), tone("error")), ("attention", "serious"))

    def test_a_deploy_verified_is_good_one_that_failed_its_checks_serious_one_that_ran_none_toneless(self):
        def tone(*conclusions):
            verified = [{"name": "Verify", "conclusion": conclusion} for conclusion in conclusions]
            return repository(deployments=[{"environment": "production", "verified": verified}]).verified_tone

        self.assertEqual((tone("success"), tone("success", "failure"), tone()), ("good", "serious", ""))

    def test_alerts_are_serious_by_a_serious_one_attention_by_any_good_when_clear_and_toneless_unoffered(self):
        def tone(**counts):
            return repository(alerts={"dependabot": counts}).alerts_tone

        self.assertEqual((tone(high=1, low=2), tone(low=1), tone(low=0)), ("serious", "attention", "good"))
        self.assertEqual(repository().alerts_tone, "")
