"""The development servers' two questions: a signed-in session, and where a page lives."""

import importlib
import os
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, modify_settings, override_settings
from django.urls import clear_url_caches, reverse

from hq.config import urls
from hq.platform.application.plugins import clear_plugin_composition_cache

EXAMPLE = "tests.fixtures.example_hq_plugin"


def run(*args: str) -> str:
    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


class DevSessionTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user("operator")

    @override_settings(DEBUG=True)
    def test_the_printed_cookie_signs_a_browser_in(self):
        name, key = run("dev_session", "operator").strip().split("=", 1)
        self.assertEqual(name, settings.SESSION_COOKIE_NAME)

        self.client.cookies[name] = key
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user.get_username(), "operator")

    @override_settings(DEBUG=True)
    def test_an_unknown_account_is_refused_and_none_is_made(self):
        with self.assertRaisesMessage(CommandError, "nobody"):
            run("dev_session", "nobody")
        self.assertFalse(get_user_model().objects.filter(username="nobody").exists())

    def test_it_refuses_without_debug(self):
        with self.assertRaisesMessage(CommandError, "DEBUG"):
            run("dev_session", "operator")


class RoutesTests(SimpleTestCase):
    def test_each_named_route_is_listed_with_its_path(self):
        lines = run("routes").splitlines()

        self.assertIn(["dashboard", reverse("dashboard")], [line.split() for line in lines])

    def test_a_filter_keeps_the_routes_that_match_it(self):
        listed = [line.split()[0] for line in run("routes", "health").splitlines()]

        self.assertIn("health_live", listed)
        self.assertNotIn("dashboard", listed)


def _remount() -> None:
    clear_plugin_composition_cache()
    importlib.reload(urls)
    clear_url_caches()


@modify_settings(INSTALLED_APPS={"append": EXAMPLE})
class ExtensionRoutesTests(SimpleTestCase):
    def setUp(self):
        composed = mock.patch.dict(
            os.environ,
            {"SEVERINO_HQ_PLUGINS": f"{EXAMPLE}.plugin:plugin", "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0"},
        )
        composed.start()
        self.addCleanup(_remount)
        self.addCleanup(composed.stop)
        _remount()

    def test_an_extensions_routes_are_listed_under_its_mount(self):
        listed = [line.split() for line in run("routes", "example_plugin").splitlines()]

        self.assertEqual(listed, [["example_plugin:index", "/examples/notes/"]])
