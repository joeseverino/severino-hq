from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import REPOSITORY_KIND

from ..dashboard import work_queue
from ..freshness import stale_after
from ..github_estate import attention, repository_for
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
