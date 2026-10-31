"""The theme switch: system, light or dark, drawn server-side on `<html>`."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse

from hq.platform.core.context_processors import appearance
from hq.platform.core.models import Appearance, AuditLog

from ..appearance import SET_THEME, ThemeCommand, set_theme, theme_for
from ..capabilities import execute_capability
from ..security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal, web_principal
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


def denials() -> int:
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SET_THEME).count()


class ThemeServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.person = web_principal(self.user)

    def choose(self, theme, principal=None):
        return execute_capability(SET_THEME, {"theme": theme}, principal=principal or self.person)

    def test_nobody_has_chosen_until_they_choose(self):
        self.assertEqual(theme_for(self.user), "system")
        self.assertFalse(Appearance.objects.exists())

    def test_anybody_signed_out_follows_the_system(self):
        # The context processor passes signed_in(request), which is None for
        # anybody signed out.
        self.assertEqual(theme_for(None), "system")

    def test_a_signed_out_page_prints_its_theme(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        context = appearance(request)

        self.assertEqual(Template("{{ THEME }}").render(Context(context)), "system")

    def test_a_choice_is_kept_and_recorded_once(self):
        self.choose("dark")
        self.choose("dark")

        self.assertEqual(theme_for(self.user), "dark")
        (event,) = AuditLog.objects.filter(object_type="Appearance")
        self.assertEqual((event.message, event.metadata["operation"]), ("Theme set to Dark", SET_THEME))

    def test_no_credential_can_choose_for_a_person(self):
        granted = frozenset(Capability)
        for principal in (
            Principal("example-agent", "mcp", granted, granted=granted),
            Principal("example-client", "api", granted, granted=granted),
            cli_principal(),
        ):
            with self.subTest(interface=principal.interface):
                self.assertEqual(self.choose("dark", principal)["error"]["code"], "forbidden")
        self.assertEqual(theme_for(self.user), "system")
        self.assertEqual(denials(), 3)

    def test_an_unknown_theme_is_refused(self):
        for theme in ("", "sepia", "DARK", "system "):
            with self.subTest(theme=theme):
                self.assertEqual(
                    self.choose(theme)["error"], {"code": "invalid_input", "message": "Choose system, light or dark."}
                )
        self.assertFalse(Appearance.objects.exists())

    def test_the_choice_does_not_happen_without_its_record(self):
        with (
            mock.patch("hq.platform.application.appearance.record_event", side_effect=RuntimeError("audit down")),
            self.assertRaises(RuntimeError),
        ):
            set_theme(ThemeCommand("dark"), principal=self.person)
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
                self.assertEqual(refused.content, b"Choose system, light or dark.")
        self.assertEqual(self.client.post(reverse("theme")).status_code, 400)
        self.assertEqual(theme_for(self.user), "dark")

    def test_an_operator_without_the_grant_is_refused_with_the_reason(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.SET_PREFERENCES})
        with mock.patch("hq.platform.core.views.web_principal", return_value=lacking):
            refused = self.choose("dark")

        self.assertEqual(refused.status_code, 403)
        self.assertIn(b"set_preferences", refused.content)
        self.assertEqual(theme_for(self.user), "system")
        self.assertEqual(denials(), 1)

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
