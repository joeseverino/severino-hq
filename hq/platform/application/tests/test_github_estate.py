from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

from ..github_estate import attention, repository_for


def store(**record):
    ProviderInventory.objects.update_or_create(
        kind="github.repository",
        defaults={
            "records": [{"connection_ref": "github", "repository": "example/alpha", "url": "https://github.com/example/alpha",
                         "default_branch": "main", "head": {"sha": "abc"}, **record}],
            "reachable": True, "connected": True, "observed_at": timezone.now(),
            "refused_parts": [{"part": "dependabot", "scope": "example/alpha"}],
        },
    )


class EstateTests(TestCase):
    def test_a_project_joins_its_repository_by_url(self):
        store()

        found = repository_for("https://github.com/example/alpha")

        self.assertEqual((found.name, found.refused), ("example/alpha", ("dependabot",)))
        self.assertIsNone(repository_for("https://github.com/example/other"))

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

        self.assertEqual(set(items), {
            "github-waiting:example/alpha:9",
            "github-failing:example/alpha",
            "github-alerts:example/alpha",
            "github-artifact:example/alpha:alpha-admission-abc",
        })
        self.assertEqual(items["github-waiting:example/alpha:9"].status, "serious")  # held for hours
        self.assertEqual(items["github-alerts:example/alpha"].title, "1 serious alert in alpha")
        self.assertEqual(items["github-artifact:example/alpha:alpha-admission-abc"].status, "attention")

    def test_a_quiet_repository_asks_for_nothing(self):
        store(checks={"state": "success"}, alerts={"code_scanning": {"low": 2}})

        self.assertEqual(attention(), ())


class VerificationTests(TestCase):
    def test_a_deploy_that_failed_its_own_verification_is_serious(self):
        store(deployments=[{"environment": "production", "url": "u", "verified": [{"name": "Verify the image was signed", "conclusion": "failure"}]}])

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
