from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import PARTS, REPOSITORY_KIND

from ..dashboard import work_queue
from ..derivation_testing import frozen
from ..freshness import stale_after
from ..github_estate import ALERT_PAGES, Repository, attention, repository_for, workflow_run
from ..item_help import RUN


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

    def test_what_waits_on_a_person_becomes_the_action_queue(self):
        long_ago = (timezone.now() - timedelta(hours=3)).isoformat()
        soon = (timezone.now() + timedelta(days=5)).isoformat()
        store(
            checks={"state": "failure", "failing": ["lint"]},
            waiting=[{"id": 9, "name": "Compose", "environments": ["production"], "created_at": long_ago, "url": "u"}],
            alerts={"code_scanning": {"critical": 1, "low": 4}},
            artifacts=[{"name": "alpha-admission-abc", "expires_at": soon}],
        )

        items = {item.key: item for item in attention()}

        self.assertEqual(
            set(items),
            {
                "github-waiting:example/alpha:9",
                "github-failing:example/alpha",
                "github-alerts:example/alpha",
                "github-artifact:example/alpha:alpha-admission-abc",
            },
        )
        self.assertEqual(items["github-waiting:example/alpha:9"].status, "serious")  # held for hours
        self.assertEqual(items["github-alerts:example/alpha"].title, "1 serious alert in alpha")
        self.assertEqual(items["github-artifact:example/alpha:alpha-admission-abc"].status, "attention")

    def test_a_quiet_repository_asks_for_nothing(self):
        store(checks={"state": "success"}, alerts={"code_scanning": {"low": 2}})

        self.assertEqual(attention(), ())


def commands(item) -> list[str]:
    return [step.summary for step in item.workflow.steps if step.phase == RUN]


class FailingRunTests(TestCase):
    RUN = "https://github.com/example/alpha/actions/runs/7"

    def failing(self, *runs):
        store(checks={"state": "failure", "failing": ["lint"]}, runs=list(runs))
        (item,) = attention()
        return item

    def test_the_item_opens_the_run_that_failed_on_the_head(self):
        item = self.failing(
            {"id": 7, "name": "CI", "conclusion": "failure", "sha": "abc", "url": self.RUN},
            {"id": 8, "name": "Docs", "conclusion": "success", "sha": "abc", "url": f"{self.RUN}8"},
            {"id": 9, "name": "Nightly", "conclusion": "failure", "sha": "old", "url": f"{self.RUN}9"},
        )

        self.assertEqual(item.url, self.RUN)
        self.assertEqual(commands(item), [])

    def test_several_failing_runs_are_each_linked(self):
        item = self.failing(
            {"id": 7, "name": "CI", "conclusion": "failure", "sha": "abc", "url": self.RUN},
            {"id": 8, "name": "Release", "conclusion": "timed_out", "sha": "abc", "url": f"{self.RUN}8"},
        )

        self.assertEqual([link.url for link in item.links], [self.RUN, f"{self.RUN}8"])

    def test_a_run_address_that_is_not_a_web_address_is_never_a_link(self):
        item = self.failing({"id": 7, "name": "CI", "conclusion": "failure", "sha": "abc", "url": "javascript:x"})

        self.assertEqual(item.url, "https://github.com/example/alpha/commit/abc")

    def test_without_the_run_in_the_reading_the_item_offers_the_command_that_lists_it(self):
        item = self.failing()

        (command,) = commands(item)
        self.assertIn("gh run list -R example/alpha --commit abc", command)


class VerificationTests(TestCase):
    def test_a_deploy_that_failed_its_own_verification_is_serious(self):
        store(
            deployments=[
                {
                    "environment": "production",
                    "url": "u",
                    "verified": [{"name": "Verify the image was signed", "conclusion": "failure"}],
                }
            ]
        )

        (item,) = attention()

        self.assertEqual((item.key, item.status), ("github-unverified:example/alpha", "serious"))
        self.assertIn("Verify the image was signed", item.body)


class ProjectPushedTests(TestCase):
    def test_a_project_with_a_read_repository_says_when_it_was_pushed(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from hq.domains.projects.models import Project

        store(pushed_at=(timezone.now() - timedelta(minutes=5)).isoformat())
        project = Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha")
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))

        response = self.client.get(reverse("projects:detail", args=[project.slug]))

        self.assertContains(response, "Last push 5")
        self.assertNotContains(response, "Edited ")


def waiting_deploy() -> None:
    started = (timezone.now() - timedelta(hours=3)).isoformat()
    store(waiting=[{"id": 9, "name": "Deploy", "environments": ["production"], "created_at": started, "url": "u"}])


def read(ago: timedelta) -> None:
    ProviderInventory.objects.filter(kind=REPOSITORY_KIND).update(observed_at=timezone.now() - ago)


class ReadingAgeTests(TestCase):
    """An item says how old the reading behind it is, and an old reading
    never stands in the queue as something happening now."""

    def test_an_item_from_a_current_reading_says_the_condition_holds_and_when_it_was_read(self):
        waiting_deploy()
        read(timedelta(minutes=5))

        (item,) = attention()

        self.assertEqual((item.status, item.notice), ("serious", False))
        self.assertIn("waits for your approval", item.title)
        self.assertAlmostEqual(item.observed_at, timezone.now() - timedelta(minutes=5), delta=timedelta(minutes=1))

    def test_an_item_from_an_out_of_date_reading_is_a_notice_of_what_was_read(self):
        waiting_deploy()
        read(stale_after(REPOSITORY_KIND) + timedelta(hours=1))

        (item,) = attention()
        queued = [entry for entry in work_queue() if entry["key"].endswith(item.key)]

        self.assertTrue(item.notice)
        self.assertNotEqual(item.status, "serious")
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

        (item,) = attention()

        self.assertIn("1 has no update pull request", item.body)
        self.assertEqual(item.url, "/projects/alpha/#updates")
        self.assertEqual([link.url for link in item.links], [f"{ALPHA}/security/dependabot"])

    def test_with_every_serious_alert_answered_the_item_opens_that_parts_alerts(self):
        store(alerts={"dependabot": {"critical": 1}}, pull_requests=[UPDATE], dependabot_alerts=[ANSWERED])

        (item,) = attention()

        self.assertNotIn("update pull request", item.body)
        self.assertEqual((item.url, item.links), (f"{ALPHA}/security/dependabot", ()))

    def test_serious_alerts_in_several_parts_link_each_parts_list_the_worst_first(self):
        store(alerts={"dependabot": {"high": 1}, "code_scanning": {"critical": 1}, "leaked_credentials": {"leaked": 1}})

        (item,) = attention()

        self.assertEqual(item.url, f"{ALPHA}/security/code-scanning")
        self.assertEqual(
            [link.url for link in item.links],
            [f"{ALPHA}/security/code-scanning", f"{ALPHA}/security/dependabot", f"{ALPHA}/security/secret-scanning"],
        )


class LeakedCredentialItemTests(TestCase):
    def test_a_leaked_credential_alone_is_a_serious_item_that_opens_secret_scanning(self):
        store(alerts={"leaked_credentials": {"leaked": 1}})

        (item,) = attention()

        self.assertEqual(item.status, "serious")
        self.assertEqual(item.title, "1 leaked credential in alpha")
        self.assertEqual(item.url, f"{ALPHA}/security/secret-scanning")

    def test_a_leaked_credential_beside_a_high_alert_is_one_item_linking_each_kind(self):
        store(alerts={"leaked_credentials": {"leaked": 1}, "dependabot": {"high": 1}})

        (item,) = attention()

        self.assertEqual(item.value, "2")
        self.assertIn("1 is a leaked credential", item.body)
        self.assertEqual(
            {link.url for link in item.links}, {f"{ALPHA}/security/dependabot", f"{ALPHA}/security/secret-scanning"}
        )

    def test_no_open_alert_raises_no_item(self):
        store(alerts={"leaked_credentials": {"leaked": 0}, "dependabot": {}})

        self.assertEqual(attention(), ())


ALPHA = "https://github.com/example/alpha"


class AlertPageTests(SimpleTestCase):
    def test_every_part_that_reads_alerts_has_its_list_on_github(self):
        self.assertEqual(set(ALERT_PAGES), {part for part in PARTS if part.label.endswith(" alerts")})

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

        self.assertEqual((run.state, run.tone), ("Timed out", "serious"))
        self.assertTrue(run.tip.startswith("Timed out\nmain · abcdef1 · "))
        self.assertTrue(run.said.startswith("timed out, main, "))

    def test_a_run_still_going_asks_for_attention(self):
        run = workflow_run({"name": "CI", "status": "in_progress"})

        self.assertEqual((run.state, run.tone, run.tip), ("In progress", "attention", "In progress"))

    def test_a_finished_run_with_no_verdict_has_no_tone(self):
        self.assertEqual(workflow_run({"status": "completed", "conclusion": "skipped"}).tone, "")

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
