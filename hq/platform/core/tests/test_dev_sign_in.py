"""The scratch server's sign-in link: routed there alone, for its throwaway account alone, once, briefly."""

import importlib
import json
import subprocess
import sys
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, clear_url_caches, resolve, reverse

from hq.config import urls
from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

ACCOUNT = "operator"
SIGN_IN = "/dev/sign-in/token/"
SCRATCH = {"HQ_DEV_PROFILE": "scratch", "DJANGO_DEBUG": "1", "UVICORN_HOST": "127.0.0.1", "HQ_DEV_ACCOUNT": ACCOUNT}

# Loads the settings and the URL configuration as a server started under the
# given environment does, outside the suite, and says whether the link is routed.
PROBE = f"""
import json, os, django
os.environ["DJANGO_SETTINGS_MODULE"] = "hq.config.settings"
django.setup()
from django.urls import resolve, Resolver404
try:
    resolve({SIGN_IN!r})
    routed = True
except Resolver404:
    routed = False
print(json.dumps(routed))
"""


def routed_under(**env: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=subprocess_env(
            drop=("HQ_DEV_PROFILE", "HQ_DEV_ACCOUNT", "UVICORN_HOST", "DJANGO_DEBUG"),
            DJANGO_SECRET_KEY="sign-in-probe-key-0123456789abcdef0123456789abcdef",
            **env,
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class RoutedTests(SimpleTestCase):
    def test_a_scratch_server_on_loopback_routes_it(self):
        self.assertTrue(routed_under(**SCRATCH))

    def test_no_other_server_routes_it(self):
        for name, env in {
            "dev": {**SCRATCH, "HQ_DEV_PROFILE": "secure", "DJANGO_DEBUG": "0"},
            "production": {"DJANGO_DEBUG": "0", "HQ_DEV_ACCOUNT": ACCOUNT},
            "a wide bind": {**SCRATCH, "UVICORN_HOST": "0.0.0.0"},
        }.items():
            with self.subTest(name):
                self.assertFalse(routed_under(**env))

    def test_the_suite_does_not_route_it(self):
        with self.assertRaises(Resolver404):
            resolve(SIGN_IN)

    def test_every_condition_is_required(self):
        from hq.config.devtools import scratch_account

        loopback = {"debug": True, "profile": "scratch", "host": "127.0.0.1", "account": ACCOUNT, "testing": False}
        self.assertEqual(scratch_account(**loopback), ACCOUNT)
        self.assertEqual(scratch_account(**{**loopback, "host": "::1"}), ACCOUNT)
        for name, change in {
            "DEBUG off": {"debug": False},
            "the dev profile": {"profile": "secure"},
            "no profile": {"profile": ""},
            "the suite": {"testing": True},
            "every address": {"host": "0.0.0.0"},
            "a tailnet address": {"host": "100.64.0.1"},
            "a public address": {"host": "192.0.2.10"},
            "a name": {"host": "hq.example.com"},
        }.items():
            with self.subTest(name):
                self.assertEqual(scratch_account(**{**loopback, **change}), "")


def _remount() -> None:
    importlib.reload(urls)
    clear_url_caches()


class SignInLinkTests(TestCase):
    def setUp(self):
        # Registered first, so it runs once the setting is back off.
        self.addCleanup(_remount)
        self.enterContext(override_settings(SEVERINO_SCRATCH_ACCOUNT=ACCOUNT))
        _remount()
        users = get_user_model().objects
        self.account = users.create_user(ACCOUNT)
        self.other = users.create_user("someone-else")

    def link(self, user=None) -> str:
        from hq.platform.core.dev_sign_in import mint

        return reverse("scratch_sign_in", args=[mint(user or self.account)])

    def assert_signed_out(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_the_link_signs_the_account_in_and_lands_on_the_dashboard(self):
        response = self.client.get(self.link())

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(response.wsgi_request.user, self.account)

    def test_it_works_once(self):
        link = self.link()
        self.client.get(link)
        self.client.logout()

        self.assertEqual(self.client.get(link).status_code, 403)
        self.assert_signed_out()

    def test_it_expires(self):
        from hq.platform.core.dev_sign_in import LIFETIME

        made = time.time() - LIFETIME.total_seconds() - 1
        with mock.patch("django.core.signing.time.time", return_value=made):
            link = self.link()

        self.assertEqual(self.client.get(link).status_code, 403)
        self.assert_signed_out()

    def test_it_signs_in_the_scratch_account_alone(self):
        self.assertEqual(self.client.get(self.link(self.other)).status_code, 403)
        self.assert_signed_out()

    def test_a_token_not_made_for_it_signs_nobody_in(self):
        for name, token in {
            "garbage": "token",
            "another salt": signing.dumps([self.account.pk, ""], salt="example.other", compress=False),
            "another key": signing.dumps(
                [self.account.pk, ""], key="example-other-key", salt="hq.platform.core.dev_sign_in", compress=False
            ),
        }.items():
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse("scratch_sign_in", args=[token])).status_code, 403)
                self.assert_signed_out()

    def test_only_a_caller_on_loopback_is_answered(self):
        link = self.link()

        # A tailnet caller, whom every other page answers.
        self.assertEqual(self.client.get(link, REMOTE_ADDR="100.64.0.1").status_code, 404)
        self.assertRedirects(self.client.get(link), reverse("dashboard"))

    def test_a_speculative_request_spends_nothing(self):
        link = self.link()

        self.assertEqual(self.client.get(link, headers={"Sec-Purpose": "prefetch"}).status_code, 503)
        self.assertRedirects(self.client.get(link), reverse("dashboard"))

    def test_it_declares_what_it_writes_without_a_session(self):
        from hq.platform.core.speculation import sessionless_views
        from hq.platform.core.write_bounds import declared

        views = dict(sessionless_views())
        self.assertIn(SIGN_IN.replace("token", "*"), views)
        self.assertTrue(declared(views[SIGN_IN.replace("token", "*")]))

    def test_the_printed_link_names_the_servers_address_and_signs_in(self):
        from hq.config.dev_asgi import sign_in_link

        with mock.patch.dict("os.environ", {"UVICORN_PORT": "8123"}):
            printed = sign_in_link()

        # localhost, which the server answers on both loopback stacks.
        origin = "http://localhost:8123"
        self.assertTrue(printed.startswith(f"{origin}/"))
        self.assertRedirects(self.client.get(printed.removeprefix(origin)), reverse("dashboard"))
