from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.platform.core.models import LinkedAccount

from .. import github_profile
from ..security import AuthorizationError, Principal

PROFILE = {
    "login": "example-user", "name": "Example User", "bio": "Builds things", "url": "https://github.com/example-user",
    "followers": 3, "following": 4, "public_repos": 5, "avatar": "", "starred": 1,
    "watched": [{
        "name": "example/tool", "url": "https://github.com/example/tool", "description": "A tool",
        "language": "Go", "stars": 10, "starred_at": "2026-09-20T00:00:00Z",
        "release": {"tag": "v1.2.0", "url": "https://github.com/example/tool/releases/v1.2.0",
                    "published_at": timezone.now().isoformat()},
        "advisories": [{"id": "CVE-2026-0001", "severity": "high", "summary": "A flaw",
                        "url": "https://github.com/advisories/GHSA-x", "published_at": timezone.now().isoformat()}],
    }],
}


def hold(profile=PROFILE, *, at=None, **fields):
    """Store a ``github.profile`` reading as a sweep would have."""

    from hq.domains.control_plane.models import ProviderInventory

    snapshot, _ = ProviderInventory.objects.update_or_create(
        kind=github_profile.KIND,
        defaults={"records": [profile] if profile else [], "observed_at": at or timezone.now(), **fields},
    )
    if at is not None:
        ProviderInventory.objects.filter(pk=snapshot.pk).update(updated_at=fields.get("updated_at", at))
    return snapshot


def operator():
    from ..security import cli_principal

    return cli_principal()


class PlanTests(TestCase):
    """HQ holds the profile's clock: the controller reads when the plan says."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("operator")
        LinkedAccount.objects.create(user=self.user, provider="github", login="example-user")

    def test_no_linked_account_is_nothing_to_read(self):
        LinkedAccount.objects.all().delete()

        self.assertEqual(github_profile.plan(), {"accounts": [], "due": False})

    def test_an_account_never_read_is_due(self):
        self.assertEqual(github_profile.plan(), {"accounts": ["example-user"], "due": True})

    def test_a_fresh_reading_is_carried(self):
        hold()

        self.assertEqual(github_profile.plan(), {"accounts": ["example-user"], "due": False})

    def test_a_reading_past_its_clock_is_due(self):
        hold(at=timezone.now() - github_profile.PROFILE_EVERY)

        self.assertTrue(github_profile.plan()["due"])

    def test_a_read_that_just_failed_is_not_tried_on_every_sweep(self):
        old = timezone.now() - github_profile.PROFILE_EVERY
        hold(at=old, updated_at=timezone.now(), reachable=False, error="GitHub API returned HTTP 502.")

        self.assertFalse(github_profile.plan()["due"])
        self.assertTrue(github_profile.plan(timezone.now() + github_profile.RETRY_AFTER)["due"])

    def test_asking_makes_it_due_whatever_its_age(self):
        hold()

        asked = github_profile.request_read("example-user", principal=operator())

        self.assertTrue(github_profile.plan()["due"])
        self.assertEqual(github_profile.asked_at(), asked)
        self.assertEqual(github_profile.standing().state, "queued")

    def test_the_controller_registry_carries_the_plan(self):
        from ..controller import controller_registry

        self.assertEqual(
            controller_registry()["github_profiles"], {"accounts": ["example-user"], "due": True}
        )

    def test_asking_needs_leave_to_read_public_records(self):
        reader = Principal(actor="reader", interface="web", capabilities=frozenset())
        with self.assertRaises(AuthorizationError):
            github_profile.request_read("example-user", principal=reader)

    def test_only_an_account_a_sign_in_names_is_asked_for(self):
        with self.assertRaises(ValueError):
            github_profile.request_read("someone-else", principal=operator())

    def test_the_profiles_own_age_never_makes_a_sweep_due(self):
        from hq.domains.control_plane.models import ProviderInventory

        from ..cadence import sweep_due

        ProviderInventory.objects.create(kind="adguard.rewrite", observed_at=timezone.now())
        hold(at=timezone.now() - github_profile.PROFILE_EVERY * 2)

        self.assertFalse(sweep_due()["due"])


class IngestTests(TestCase):
    """A sweep between two reads carries the profile: nothing HQ holds moves."""

    def record(self, report):
        from ..inventory import record_inventory

        return record_inventory({github_profile.KIND: report}, principal=operator())

    def test_a_carried_kind_keeps_its_records_and_its_moment(self):
        before = hold(at=timezone.now() - github_profile.RETRY_AFTER)

        result = self.record({"ok": True, "records": [], "carried": True})

        before.refresh_from_db()
        self.assertEqual(result["recorded"], [])
        self.assertEqual(before.records, [PROFILE])
        self.assertLess(before.updated_at, timezone.now() - github_profile.RETRY_AFTER / 2)

    def test_a_read_the_allowance_refused_is_an_attempt_that_keeps_the_reading(self):
        before = hold(at=timezone.now() - github_profile.RETRY_AFTER)
        refusal = {"part": "", "refusal": "", "scope": "", "connection_ref": "",
                   "reason": "GitHub allows this address 2 more anonymous calls until 18:46 UTC"}

        self.record({"ok": True, "records": [], "carried": True, "refused_parts": [refusal]})

        before.refresh_from_db()
        self.assertEqual(before.records, [PROFILE])
        self.assertEqual(before.refused_parts[0]["reason"], refusal["reason"])
        self.assertGreater(before.updated_at, before.observed_at)

    def test_a_read_replaces_the_reading(self):
        hold(at=timezone.now() - github_profile.PROFILE_EVERY)

        self.record({"ok": True, "records": [{**PROFILE, "followers": 9, "token": "dropped"}]})

        found = github_profile.profile("Example-User")
        self.assertEqual(found["followers"], 9)
        self.assertNotIn("token", found)
        self.assertEqual(found["watched"][0]["release"]["tag"], "v1.2.0")

    def test_a_record_over_the_contracts_bounds_is_refused(self):
        self.record({"ok": True, "records": [{**PROFILE, "watched": PROFILE["watched"] * 16}]})

        self.assertIsNone(github_profile.profile("example-user"))


class NoOutboundReadTests(TestCase):
    def test_the_module_cannot_reach_github(self):
        self.assertFalse(hasattr(github_profile, "read"))
        self.assertFalse(hasattr(github_profile, "refresh"))


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", is_staff=True)
        self.client.force_login(self.user)

    def test_without_a_linked_account_it_says_how_to_link_one(self):
        response = self.client.get(reverse("watching"))

        self.assertContains(response, "Your sign-in names no GitHub account.")
        self.assertNotContains(response, reverse("watching_refresh"))

    def test_before_the_first_read_it_offers_one(self):
        LinkedAccount.objects.create(user=self.user, provider="github", login="example-user")

        response = self.client.get(reverse("watching"))

        self.assertContains(response, "Not read from GitHub yet.")
        self.assertContains(response, reverse("watching_refresh"))

    def test_refresh_asks_the_controller_and_answers_at_once(self):
        LinkedAccount.objects.create(user=self.user, provider="github", login="example-user")
        hold()

        response = self.client.post(reverse("watching_refresh"), headers={"x-requested-with": "XMLHttpRequest"})

        self.assertEqual(response.status_code, 202)
        answer = response.json()
        self.assertEqual((answer["state"], answer["live"]), ("queued", True))
        self.assertEqual(self.client.get(answer["status"]).json()["state"], "queued")
        # The reading is as it was: nothing was read in the request.
        self.assertEqual(github_profile.profile("example-user")["followers"], 3)

    def test_without_script_refresh_returns_to_a_page_that_says_it_is_in_hand(self):
        LinkedAccount.objects.create(user=self.user, provider="github", login="example-user")
        hold()

        response = self.client.post(reverse("watching_refresh"), follow=True)

        self.assertRedirects(response, reverse("watching"))
        self.assertContains(response, "Reading @example-user from GitHub.")
        self.assertContains(response, "data-ask-status=")
        self.assertContains(response, 'aria-disabled="true"')
        self.assertContains(response, "Waiting for the controller.")

    def test_the_status_says_when_the_read_landed_or_was_refused(self):
        LinkedAccount.objects.create(user=self.user, provider="github", login="example-user")
        hold()
        status = self.client.post(
            reverse("watching_refresh"), headers={"x-requested-with": "XMLHttpRequest"}
        ).json()["status"]

        hold({**PROFILE, "followers": 4})
        self.assertEqual(self.client.get(status).json()["state"], "done")
        self.assertNotContains(self.client.get(reverse("watching")), "data-ask-status=")

        hold(reachable=False, error="GitHub profile: provider answered 502")
        failed = self.client.get(status).json()
        self.assertEqual((failed["state"], failed["note"]), ("failed", "GitHub profile: provider answered 502"))

    def test_a_status_hq_did_not_sign_is_not_found(self):
        self.assertEqual(self.client.get(reverse("control_plane:read_status"), {"watch": "forged"}).status_code, 404)

    def test_it_is_clearly_your_profile(self):
        LinkedAccount.objects.create(user=self.user, provider="github", login="example-user")
        hold()

        response = self.client.get(reverse("watching"))

        self.assertContains(response, "@example-user")
        self.assertContains(response, "This is your GitHub account, according to your sign-in.")
        self.assertContains(response, 'href="https://github.com/example-user?tab=followers"')
        self.assertContains(response, "v1.2.0")
        self.assertContains(response, "CVE-2026-0001")
        # The advisory says what it is about, where a phone can read it.
        self.assertContains(response, "A flaw")


class CardTests(TestCase):
    def test_the_dashboard_card_counts_what_is_new(self):
        from ..sections import watching

        user = get_user_model().objects.create_user("operator")
        LinkedAccount.objects.create(user=user, provider="github", login="example-user")
        hold()

        cards = watching()

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card["label"], card["value"]), ("New security advisories", "1"))
        self.assertNotIn("detail", card)

    def test_no_linked_account_no_card(self):
        from ..sections import watching

        self.assertEqual(watching(), ())


class ShortAgeTests(TestCase):
    def test_an_age_is_one_unit(self):
        from datetime import timedelta

        from ..moments import ago

        self.assertEqual(ago(timezone.now() - timedelta(days=5, hours=15)), "5\xa0days ago")


class ShortAgeFilterTests(TestCase):
    def test_an_iso_stamp_reads_like_a_datetime(self):
        from datetime import timedelta

        from hq.platform.core.templatetags.value_tags import ago_short

        stamp = (timezone.now() - timedelta(days=3)).isoformat()
        self.assertEqual(ago_short(stamp), "3\xa0days ago")
        self.assertEqual(ago_short(""), "")


class AppProofTests(TestCase):
    def test_the_app_reading_your_repositories_is_a_second_proof(self):
        from hq.domains.control_plane.models import ProviderInventory

        user = get_user_model().objects.create_user("operator", is_staff=True)
        self.client.force_login(user)
        LinkedAccount.objects.create(user=user, provider="github", login="example-user")
        hold()
        ProviderInventory.objects.create(
            kind="github.repository", reachable=True, connected=True, observed_at=timezone.now(),
            records=[{"connection_ref": "github", "repository": "example-user/tool", "private": True},
                     {"connection_ref": "github", "repository": "someone-else/thing"}],
        )

        response = self.client.get(reverse("watching"))

        self.assertContains(response, "HQ's GitHub App is installed on it.")
        self.assertContains(response, "reading 1 of your repositories")
        self.assertContains(response, "(private)")
        self.assertNotContains(response, "someone-else/thing")
