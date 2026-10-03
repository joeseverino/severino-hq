"""A person's picture: fetched from the provider's own origin, kept, and served only to them."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog, Avatar
from core.oidc import SIGNED_IN_SESSION_KEY, HQOIDCAuthenticationBackend

from .. import avatars
from ..avatars import MAX_BYTES, SESSION_KEY, fetch_picture, picture_allowed, remember_avatar

ISSUER = "https://sso.example.test"
PICTURE = f"{ISSUER}/api/users/1/profile-picture.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class _Reply:
    def __init__(self, body: bytes, status: int = 200):
        self.status_code = status
        self.raw = mock.Mock()
        self.raw.read = lambda amount, decode_content=True: body[:amount]


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
    def _fetch(self, reply):
        with mock.patch.object(avatars.requests, "get", return_value=reply) as get:
            return fetch_picture(PICTURE, access_token="token"), get

    def test_an_image_comes_back_as_what_its_bytes_say_it_is(self):
        fetched, get = self._fetch(_Reply(JPEG))

        self.assertEqual(fetched, ("image/jpeg", JPEG))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue(get.call_args.kwargs["timeout"])

    def test_a_redirect_is_not_followed_and_is_no_picture(self):
        fetched, _get = self._fetch(_Reply(b"", status=302))

        self.assertIsNone(fetched)

    def test_something_that_is_not_an_image_is_not_kept(self):
        for body in (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", b"<html>", b""):
            with self.subTest(body):
                self.assertIsNone(self._fetch(_Reply(body))[0])

    def test_a_file_past_the_limit_is_not_kept_and_no_more_of_it_is_read(self):
        asked = []
        reply = _Reply(PNG)
        reply.raw.read = lambda amount, decode_content=True: asked.append(amount) or PNG + b"\x00" * amount

        self.assertIsNone(self._fetch(reply)[0])
        self.assertEqual(asked, [MAX_BYTES + 1])

    def test_a_provider_that_cannot_be_reached_is_no_picture(self):
        with mock.patch.object(avatars.requests, "get", side_effect=requests.ConnectionError):
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
        with self.settings(OIDC_ISSUER=ISSUER), mock.patch.object(
            avatars, "fetch_picture", return_value=("image/png", PNG)
        ):
            self.backend._remember_picture(self.user, "token")

        self.assertEqual(self.backend.request.session[SESSION_KEY], Avatar.objects.get().digest)

    def test_a_picture_that_breaks_does_not_break_the_sign_in(self):
        self.backend.request.session[SESSION_KEY] = "kept"
        with self.settings(OIDC_ISSUER=ISSUER), mock.patch.object(
            avatars, "fetch_picture", side_effect=RuntimeError("anything")
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

    def _menu(self) -> str:
        body = self.client.get(reverse("connection")).content.decode()
        start = body.index('class="user-menu"')
        return body[start : body.index("</details>", start)]

    def test_no_picture_draws_the_mark_and_a_plain_account_has_no_role_or_admin_link(self):
        menu = self._menu()

        self.assertIn('class="user-icon"', menu)
        self.assertNotIn("<img", menu)
        self.assertNotIn("role-mark", menu)
        self.assertNotIn(reverse("admin:index"), menu)

    def test_the_sessions_picture_is_drawn_from_hq_itself(self):
        session = self.client.session
        session[SESSION_KEY] = "a" * 64
        session.save()

        self.assertEqual(self._menu().count(f'src="{reverse("avatar", args=["a" * 64])}"'), 2)

    def test_an_admin_is_marked_and_offered_the_admin(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        menu = self._menu()

        self.assertIn('<span class="role-mark">Admin</span>', menu)
        self.assertIn(reverse("admin:index"), menu)

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

                menu = self._menu()
                self.assertIn(said, menu)
                self.assertNotIn("minute", menu)
