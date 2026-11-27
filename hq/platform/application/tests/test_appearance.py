"""The theme switch: system, light or dark, drawn server-side on `<html>`."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator

from ..appearance import SET_THEME, theme_for
from ..capabilities import execute_capability
from ..security import OPERATOR_CAPABILITIES, Capability, Principal, web_principal


def denials() -> int:
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SET_THEME).count()


class ThemeServiceTests(TestCase):
    def setUp(self):
        self.user = operator(username="op")
        self.person = web_principal(self.user)

    def choose(self, theme, principal=None):
        return execute_capability(SET_THEME, keyed({"theme": theme}), principal=principal or self.person)

    def test_a_choice_is_kept_and_recorded_once(self):
        self.choose("dark")
        self.choose("dark")

        self.assertEqual(theme_for(self.user), "dark")
        (event,) = AuditLog.objects.filter(object_type="Appearance")
        self.assertEqual(event.metadata["operation"], SET_THEME)


class ThemeSwitchTests(TestCase):
    def setUp(self):
        self.user = operator(self.client, "op")

    def choose(self, theme, **extra):
        return self.client.post(reverse("theme"), form_keyed({"theme": theme, "next": reverse("dashboard")}), **extra)

    def test_an_operator_without_the_grant_is_refused_with_the_reason(self):
        lacking = Principal("op", "web", OPERATOR_CAPABILITIES - {Capability.SET_PREFERENCES})
        with mock.patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            refused = self.choose("dark")

        self.assertEqual(refused.status_code, 403)
        self.assertIn(b"set_preferences", refused.content)
        self.assertEqual(theme_for(self.user), "system")
        self.assertEqual(denials(), 1)

    def test_the_form_is_protected_against_cross_site_posts(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("theme"), form_keyed({"theme": "dark"}))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(theme_for(self.user), "system")
