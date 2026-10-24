"""A person's picture: fetched from the provider's own origin, kept, and served only to them."""

from datetime import timedelta
from unittest import mock

import httpx2
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from hq.platform.core.models import AuditLog, Avatar
from hq.platform.core.oidc import SIGNED_IN_SESSION_KEY, HQOIDCAuthenticationBackend

from .. import avatars
from ..avatars import MAX_BYTES, SESSION_KEY, fetch_picture, picture_allowed, remember_avatar
from .markup import Element, within

ISSUER = "https://sso.example.test"
PICTURE = f"{ISSUER}/api/users/1/profile-picture.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class PictureAllowedTests(SimpleTestCase):
    def test_only_the_providers_own_origin_is_asked(self):
        self.assertTrue(picture_allowed(PICTURE, ISSUER))
        for elsewhere in (
            "https://evil.example.test/a.png",
            "http://sso.example.test/a.png",
            "https://sso.example.test:8443/a.png",
            "https://sso.example.test.evil.test/a.png",
            "https://sso.example.test@evil.test/a.png",
            "file:///etc/passwd",
            "//sso.example.test/a.png",
            "http://169.254.169.254/latest/meta-data/",
            "",
        ):
            with self.subTest(elsewhere):
                self.assertFalse(picture_allowed(elsewhere, ISSUER))

    def test_no_configured_issuer_allows_nothing(self):
        self.assertFalse(picture_allowed(PICTURE, ""))


class FetchPictureTests(SimpleTestCase):
    """The provider is answered at httpx2's transport, so redirects, headers and
    streaming are the client's own."""

    def _fetch(self, *replies: httpx2.Response, access_token: str = "token"):
        asked: list[httpx2.Request] = []

        def answer(_transport, request):
            asked.append(request)
            return replies[len(asked) - 1]

        with mock.patch.object(httpx2.HTTPTransport, "handle_request", answer):
            return fetch_picture(PICTURE, access_token=access_token), asked

    def test_an_image_comes_back_as_what_its_bytes_say_it_is_with_the_providers_token(self):
        fetched, asked = self._fetch(httpx2.Response(200, content=JPEG))

        self.assertEqual(fetched, ("image/jpeg", JPEG))
        self.assertEqual(asked[0].headers["Authorization"], "Bearer token")
        self.assertNotIn(None, asked[0].extensions["timeout"].values())

    def test_without_a_token_none_is_sent(self):
        _fetched, asked = self._fetch(httpx2.Response(200, content=PNG), access_token="")

        self.assertNotIn("Authorization", asked[0].headers)

    def test_a_redirect_is_not_followed_and_is_no_picture(self):
        moved = httpx2.Response(302, headers={"Location": "https://evil.example.test/a.png"})
        fetched, asked = self._fetch(moved, httpx2.Response(200, content=PNG))

        self.assertIsNone(fetched)
        self.assertEqual([str(request.url) for request in asked], [PICTURE])

    def test_something_that_is_not_an_image_is_not_kept(self):
        for body in (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", b"<html>", b""):
            with self.subTest(body):
                self.assertIsNone(self._fetch(httpx2.Response(200, content=body))[0])

    def test_a_file_past_the_limit_is_not_kept_and_no_more_of_it_is_read(self):
        sent = []

        def endless():
            while True:
                sent.append(PNG)
                yield PNG

        self.assertIsNone(self._fetch(httpx2.Response(200, content=endless()))[0])
        self.assertLessEqual(len(sent) * len(PNG), MAX_BYTES + 2 * len(PNG))

    def test_a_provider_that_cannot_be_reached_is_no_picture(self):
        with mock.patch.object(httpx2.HTTPTransport, "handle_request", side_effect=httpx2.ConnectError("refused")):
            self.assertIsNone(fetch_picture(PICTURE))


class RememberAvatarTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)

    def _remember(self, url=PICTURE, *, fetched=("image/png", PNG), now=None):
        with mock.patch.object(avatars, "fetch_picture", return_value=fetched) as fetch:
            digest = remember_avatar(self.user, url, issuer=ISSUER, access_token="t", now=now)
        return digest, fetch

    def test_the_picture_is_kept_and_recorded_once(self):
        digest, _fetch = self._remember()

        avatar = Avatar.objects.get(user=self.user)
        self.assertEqual((avatar.digest, avatar.content_type, bytes(avatar.image)), (digest, "image/png", PNG))
        self.assertEqual(AuditLog.objects.filter(object_type="Avatar").count(), 1)

    def test_a_renewed_session_does_not_fetch_it_again_the_same_day(self):
        first, _fetch = self._remember()
        second, fetch = self._remember()

        self.assertEqual(first, second)
        fetch.assert_not_called()

    def test_it_is_fetched_again_once_it_is_a_day_old_and_an_unchanged_one_is_not_recorded_twice(self):
        self._remember()
        _digest, fetch = self._remember(now=timezone.now() + timedelta(days=1, minutes=1))

        fetch.assert_called_once()
        self.assertEqual(AuditLog.objects.filter(object_type="Avatar").count(), 1)

    def test_a_new_picture_replaces_the_old_one(self):
        first, _fetch = self._remember()
        second, _fetch = self._remember(fetched=("image/jpeg", JPEG), now=timezone.now() + timedelta(days=2))

        self.assertNotEqual(first, second)
        self.assertEqual(Avatar.objects.get(user=self.user).content_type, "image/jpeg")
        self.assertEqual(AuditLog.objects.filter(object_type="Avatar").count(), 2)

    def test_a_picture_from_elsewhere_is_never_fetched(self):
        digest, fetch = self._remember("https://evil.example.test/a.png")

        self.assertEqual(digest, "")
        fetch.assert_not_called()
        self.assertFalse(Avatar.objects.exists())

    def test_a_failed_fetch_leaves_the_kept_picture_alone(self):
        kept, _fetch = self._remember()
        digest, _fetch = self._remember(fetched=None, now=timezone.now() + timedelta(days=2))

        self.assertEqual(digest, kept)
        self.assertTrue(Avatar.objects.filter(digest=kept).exists())

    def test_a_provider_that_names_no_picture_drops_the_kept_one(self):
        self._remember()
        digest, _fetch = self._remember("")

        self.assertEqual(digest, "")
        self.assertFalse(Avatar.objects.exists())


class SignInTests(TestCase):
    """The backend's part: the session learns which picture, and nothing here stops a sign-in."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.backend = HQOIDCAuthenticationBackend()
        self.backend.request = mock.Mock(session={})
        self.backend._userinfo = {"picture": PICTURE}

    def test_the_session_is_told_which_picture(self):
        with (
            self.settings(OIDC_ISSUER=ISSUER),
            mock.patch.object(avatars, "fetch_picture", return_value=("image/png", PNG)),
        ):
            self.backend._remember_picture(self.user, "token")

        self.assertEqual(self.backend.request.session[SESSION_KEY], Avatar.objects.get().digest)

    def test_a_picture_that_breaks_does_not_break_the_sign_in(self):
        self.backend.request.session[SESSION_KEY] = "kept"
        with (
            self.settings(OIDC_ISSUER=ISSUER),
            mock.patch.object(avatars, "fetch_picture", side_effect=RuntimeError("anything")),
        ):
            self.backend._remember_picture(self.user, "token")

        self.assertEqual(self.backend.request.session[SESSION_KEY], "kept")

    def test_no_picture_claim_clears_the_sessions_picture(self):
        self.backend._userinfo = {}
        self.backend.request.session[SESSION_KEY] = "old"
        with self.settings(OIDC_ISSUER=ISSUER):
            self.backend._remember_picture(self.user, "token")

        self.assertNotIn(SESSION_KEY, self.backend.request.session)


class AvatarViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.other = get_user_model().objects.create_user("other", password="x" * 20)
        Avatar.objects.create(user=self.user, content_type="image/png", image=PNG, digest="a" * 64, source=PICTURE)
        Avatar.objects.create(user=self.other, content_type="image/png", image=PNG, digest="b" * 64, source=PICTURE)

    def test_a_person_is_served_their_own_picture_locked_down_and_cacheable(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("avatar", args=["a" * 64]))

        self.assertEqual((response.status_code, response.content, response["Content-Type"]), (200, PNG, "image/png"))
        self.assertIn("immutable", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        self.assertEqual(response["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_nobody_is_served_somebody_elses(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get(reverse("avatar", args=["b" * 64])).status_code, 404)

    def test_signed_out_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(reverse("avatar", args=["a" * 64])).status_code, 302)


class MenuTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)

    def _menu(self) -> list[Element]:
        return within(self.client.get(reverse("connection")), "class", "user-menu")

    def test_no_picture_draws_the_mark_and_a_plain_account_has_no_role(self):
        menu = self._menu()

        drawn = set().union(*(element.classes for element in menu))
        self.assertIn("user-icon", drawn)
        self.assertNotIn("role-mark", drawn)
        self.assertNotIn("img", [element.tag for element in menu])

    def test_the_sessions_picture_is_drawn_from_hq_itself(self):
        session = self.client.session
        session[SESSION_KEY] = "a" * 64
        session.save()

        sources = [element.attrs.get("src") for element in self._menu()]
        self.assertEqual(sources.count(reverse("avatar", args=["a" * 64])), 2)

    def test_a_superuser_is_marked_and_led_nowhere_else(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        menu = self._menu()

        self.assertEqual([element.words for element in menu if "role-mark" in element.classes], ["Admin"])
        self.assertFalse(any(element.attrs.get("href", "").startswith("/admin/") for element in menu))

    def test_the_sign_in_is_aged_in_hours_or_days_never_minutes(self):
        for age, said in (
            (timedelta(minutes=7), "Signed in within the hour"),
            (timedelta(hours=1, minutes=5), "Signed in 1 hour ago"),
            (timedelta(hours=30), "Signed in 30 hours ago"),
            (timedelta(days=3, hours=2), "Signed in 3 days ago"),
        ):
            with self.subTest(said):
                session = self.client.session
                session[SIGNED_IN_SESSION_KEY] = (timezone.now() - age).isoformat()
                session.save()

                menu = " ".join(element.words for element in self._menu())
                self.assertIn(said, menu)
                self.assertNotIn("minute", menu)
