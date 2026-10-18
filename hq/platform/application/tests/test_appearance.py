"""The theme switch: system, light or dark, drawn server-side on `<html>`."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.core.models import Appearance, AuditLog

from ..appearance import set_theme, theme_for
from ..security import AuthorizationError, Capability, Principal
from .markup import elements, select


def drawn_theme(response) -> str | None:
    """The theme the page is drawn in, as the stylesheet reads it."""

    (document,) = elements(response, "html")
    return document.attrs.get("data-theme")


def color_scheme(response) -> str:
    (meta,) = select(response, "name", "color-scheme")
    return meta.attrs["content"]


def pressed(response) -> list[str]:
    """The theme the switch shows as chosen."""

    return [choice.attrs["value"] for choice in select(response, "aria-pressed", "true") if "value" in choice.attrs]


class ThemeServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.person = Principal("op", "web", frozenset())

    def test_nobody_has_chosen_until_they_choose(self):
        self.assertEqual(theme_for(self.user), "system")
        self.assertFalse(Appearance.objects.exists())

    def test_a_choice_is_kept_and_recorded_once(self):
        set_theme("dark", principal=self.person, user=self.user)
        set_theme("dark", principal=self.person, user=self.user)

        self.assertEqual(theme_for(self.user), "dark")
        self.assertEqual(
            list(AuditLog.objects.filter(object_type="Appearance").values_list("message", flat=True)),
            ["Theme set to Dark"],
        )

    def test_no_credential_can_choose_for_a_person(self):
        for interface in ("mcp", "api", "cli"):
            credential = Principal("example-agent", interface, frozenset({Capability.READ}))
            with self.subTest(interface=interface), self.assertRaises(AuthorizationError):
                set_theme("dark", principal=credential, user=self.user)
        self.assertEqual(theme_for(self.user), "system")

    def test_an_unknown_theme_is_refused(self):
        for theme in ("", "sepia", "DARK", "system "):
            with self.subTest(theme=theme), self.assertRaises(ValueError):
                set_theme(theme, principal=self.person, user=self.user)
        self.assertFalse(Appearance.objects.exists())

    def test_the_choice_does_not_happen_without_its_record(self):
        with (
            mock.patch("hq.platform.application.appearance.record_event", side_effect=RuntimeError("audit down")),
            self.assertRaises(RuntimeError),
        ):
            set_theme("dark", principal=self.person, user=self.user)
        self.assertEqual(theme_for(self.user), "system")


class ThemeSwitchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.user)

    def choose(self, theme, **extra):
        return self.client.post(reverse("theme"), {"theme": theme, "next": reverse("dashboard")}, **extra)

    def test_a_choice_is_drawn_on_the_next_page_server_side(self):
        for theme, meta in (("dark", "dark"), ("light", "light")):
            with self.subTest(theme=theme):
                response = self.choose(theme)
                self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
                page = self.client.get(reverse("dashboard"))
                self.assertEqual(drawn_theme(page), theme)
                self.assertEqual(color_scheme(page), meta)
                self.assertEqual(pressed(page), [theme])

    def test_system_removes_the_attribute(self):
        self.choose("dark")
        self.choose("system")

        page = self.client.get(reverse("dashboard"))
        self.assertIsNone(drawn_theme(page))
        self.assertEqual(color_scheme(page), "light dark")
        self.assertEqual(theme_for(self.user), "system")

    def test_invalid_input_is_refused_and_changes_nothing(self):
        self.choose("dark")
        for theme in ("sepia", "", "<script>"):
            with self.subTest(theme=theme):
                refused = self.choose(theme)
                self.assertEqual(refused.status_code, 400)
                # The reply is fixed, never the exception's text.
                self.assertEqual(refused.content, b"Choose system, light or dark.")
        self.assertEqual(self.client.post(reverse("theme")).status_code, 400)
        self.assertEqual(theme_for(self.user), "dark")

    def test_it_only_changes_on_post(self):
        self.assertEqual(self.client.get(reverse("theme"), {"theme": "dark"}).status_code, 405)
        self.assertEqual(theme_for(self.user), "system")

    def test_it_returns_only_to_this_site(self):
        response = self.client.post(reverse("theme"), {"theme": "dark", "next": "https://example.net/"})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_anybody_signed_out_is_sent_to_sign_in_and_changes_nothing(self):
        self.client.logout()
        response = self.choose("dark")

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertFalse(Appearance.objects.exists())

    def test_a_choice_belongs_to_the_person_who_made_it(self):
        self.choose("dark")
        other = get_user_model().objects.create_user("other", password="x" * 20)
        self.client.force_login(other)

        self.assertIsNone(drawn_theme(self.client.get(reverse("dashboard"))))

    def test_the_form_is_protected_against_cross_site_posts(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("theme"), {"theme": "dark"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(theme_for(self.user), "system")
