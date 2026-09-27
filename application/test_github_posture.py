from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .github_posture import attention, build_attention, postures
from .standards import MET, UNAVAILABLE, UNMET
from .test_github_estate import store

# What a well-kept repository reads as: only its owner, one read-only key in
# use, a read-only token that approves nothing, pinned actions, fixes on.
KEPT = {
    "collaborators": [{"login": "example", "role": "admin"}],
    "deploy_keys": [{"title": "deploy", "read_only": True, "last_used": timezone.now().isoformat()}],
    "token": "read",
    "token_approves_reviews": False,
    "pinning_required": True,
    "security_fixes": True,
    "security": None,
}


def kept(**changes):
    return {**KEPT, **changes}


class PostureTests(TestCase):
    def test_a_kept_private_repository_meets_the_private_standard(self):
        store(private=True, access=kept(), variables=[])

        found = postures()[0]

        self.assertEqual((found.met, found.measured, found.unmet), (8, 8, ()))
        self.assertNotIn("secret-scanning", [result.check.id for result in found.results])

    def test_a_public_repository_is_held_to_more(self):
        store(
            private=False, access=kept(security={"secret_scanning": "enabled", "secret_scanning_push_protection": "disabled"}),
            variables=[], rules={"pull_request": True, "blocks_force_push": True, "blocks_deletion": False},
            alerts={"code_scanning": {}},
        )

        found = postures()[0]

        self.assertEqual(found.state_of("secret-scanning"), MET)
        self.assertEqual(found.state_of("push-protection"), UNMET)
        self.assertEqual(found.state_of("deletion-blocked"), UNMET)
        self.assertEqual(found.state_of("code-scanning"), MET)
        self.assertEqual(len(found.results), 14)

    def test_what_hq_could_not_read_is_unavailable_not_a_failure(self):
        store(private=False)

        found = postures()[0]

        self.assertEqual(found.state_of("only-you"), UNAVAILABLE)
        self.assertEqual(found.state_of("secret-scanning"), UNAVAILABLE)
        self.assertEqual((found.measured, attention()), (0, ()))

    def test_someone_gaining_reach_is_serious_and_drift_is_not(self):
        stale = (timezone.now() - timedelta(days=200)).isoformat()
        store(private=True, variables=["DEPLOY_TARGET"], access=kept(
            collaborators=[{"login": "example", "role": "admin"}, {"login": "guest", "role": "write"}],
            deploy_keys=[{"title": "old", "read_only": False, "last_used": stale}],
            token="write",
        ))

        items = {item.key: item.status for item in attention()}

        self.assertEqual(items, {
            "github-posture:only-you": "serious",
            "github-posture:keys-read-only": "serious",
            "github-posture:keys-in-use": "attention",
            "github-posture:token-read-only": "attention",
            "github-posture:no-variables": "attention",
        })

    def test_one_item_per_gap_however_many_repositories_miss_it(self):
        from control_plane.models import ProviderInventory

        record = {"connection_ref": "github", "default_branch": "main", "head": {}, "private": True,
                  "access": kept(pinning_required=False), "variables": []}
        ProviderInventory.objects.update_or_create(kind="github.repository", defaults={
            "records": [{**record, "repository": f"example/{name}", "url": f"https://github.com/example/{name}"}
                        for name in ("alpha", "beta", "gamma")],
            "reachable": True, "connected": True, "observed_at": timezone.now(),
        })

        (item,) = attention()

        self.assertEqual((item.key, item.value, item.magnitude), ("github-posture:actions-pinned", "3", 3))
        self.assertTrue(item.body.startswith("alpha, beta, gamma."))

    def test_the_build_queue_carries_both_the_repository_and_the_standard(self):
        store(private=True, checks={"state": "failure", "failing": ["lint"]}, access=kept(token="write"), variables=[])

        keys = {item.key for item in build_attention()}

        self.assertEqual(keys, {"github-failing:example/alpha", "github-posture:token-read-only"})


class PostureViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))

    def test_the_page_leads_with_what_is_not_met(self):
        store(private=True, access=kept(token="write"), variables=["HQ_IMAGE"])

        response = self.client.get(reverse("posture"))

        gaps = [gap["check"].id for gap in response.context["unmet"]]
        self.assertEqual(gaps, ["token-read-only", "no-variables"])
        self.assertIn("only-you", [check.id for check in response.context["everywhere"]])
        self.assertContains(response, "6 of 8")
        self.assertContains(response, "HQ_IMAGE")

    def test_a_repository_joins_its_project(self):
        from projects.models import Project

        project = Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha")
        store(private=True, access=kept(), variables=[])

        response = self.client.get(reverse("posture"))

        self.assertContains(response, f'href="{project.get_absolute_url()}"')
        self.assertEqual(response.context["unmet"], [])

    def test_nothing_read_says_so(self):
        response = self.client.get(reverse("posture"))

        self.assertContains(response, "has not read any repository yet")

    def test_it_needs_a_sign_in(self):
        self.client.logout()

        response = self.client.get(reverse("posture"))

        self.assertEqual(response.status_code, 302)
