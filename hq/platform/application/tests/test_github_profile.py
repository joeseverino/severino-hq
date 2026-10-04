from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.platform.core.models import LinkedAccount

from .. import github_profile
from ..readings import record
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


class ReadTests(TestCase):
    def test_a_read_gathers_the_profile_and_each_starred_repository(self):
        answers = {
            "/users/example-user": {"login": "example-user", "name": "Example User", "followers": 3,
                                    "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4"},
            "/users/example-user/starred?per_page=100": [
                {"starred_at": "2026-09-20T00:00:00Z", "repo": {"full_name": "example/tool", "stargazers_count": 10}}
            ],
            "/repos/example/tool/releases/latest": {"tag_name": "v1.2.0"},
        }

        def fake_get(path, *, accept="", missing_ok=False):
            return answers.get(path, [] if "advisories" in path else None)

        with mock.patch.object(github_profile, "_get", side_effect=fake_get), \
                mock.patch.object(github_profile, "_avatar", return_value="data:image/png;base64,AA=="):
            found = github_profile.read("example-user")

        self.assertEqual((found["name"], found["followers"], found["starred"]), ("Example User", 3, 1))
        self.assertEqual(found["watched"][0]["release"]["tag"], "v1.2.0")
        self.assertEqual(found["avatar"], "data:image/png;base64,AA==")

    def test_an_avatar_from_anywhere_else_is_not_fetched(self):
        with mock.patch("hq.platform.application.github_profile.urllib.request.urlopen") as urlopen:
            self.assertEqual(github_profile._avatar("https://example.com/me.png"), "")
        urlopen.assert_not_called()

    def test_refreshing_needs_leave_to_read_public_records(self):
        reader = Principal(actor="reader", interface="web", capabilities=frozenset())
        with self.assertRaises(AuthorizationError):
            github_profile.refresh("example-user", principal=reader)


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

    def test_it_is_clearly_your_profile(self):
        LinkedAccount.objects.create(user=self.user, provider="github", login="example-user")
        record(github_profile.reading_key("example-user"), PROFILE)

        response = self.client.get(reverse("watching"))

        self.assertContains(response, "@example-user")
        self.assertContains(response, "Verified by your SSO sign-in")
        self.assertContains(response, 'href="https://github.com/example-user?tab=followers"')
        self.assertContains(response, "v1.2.0")
        self.assertContains(response, "CVE-2026-0001")


class CardTests(TestCase):
    def test_the_dashboard_card_counts_what_is_new(self):
        from ..sections import watching

        user = get_user_model().objects.create_user("operator")
        LinkedAccount.objects.create(user=user, provider="github", login="example-user")
        record(github_profile.reading_key("example-user"), PROFILE)

        cards = watching()

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card["label"], card["value"]), ("New advisories", "1"))
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
        record(github_profile.reading_key("example-user"), PROFILE)
        ProviderInventory.objects.create(
            kind="github.repository", reachable=True, connected=True, observed_at=timezone.now(),
            records=[{"connection_ref": "github", "repository": "example-user/tool", "private": True},
                     {"connection_ref": "github", "repository": "someone-else/thing"}],
        )

        response = self.client.get(reverse("watching"))

        self.assertContains(response, "HQ's App is installed on @example-user")
        self.assertContains(response, "reading 1 of your repositories")
        self.assertContains(response, "(private)")
        self.assertNotContains(response, "someone-else/thing")
