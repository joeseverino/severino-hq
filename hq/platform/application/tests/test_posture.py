"""HQ's standard: each check read from what decides it, shown always, raised under Advanced HQ Posture."""

import os
import subprocess
import sys

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

from ..connection_catalog import connection_catalog
from ..posture import STANDARD, estate_posture
from ..security import cli_principal
from .test_tailnet_posture import raised

RULE = "posture-unmet"
PASSWORDS = ["hq.platform.core.oidc.HQOIDCAuthenticationBackend", "django.contrib.auth.backends.ModelBackend"]
SSO_ONLY = ["hq.platform.core.oidc.HQOIDCAuthenticationBackend"]


def standing(check_id: str):
    found = estate_posture(connection_catalog(principal=cli_principal()))
    return next(standing for standing in found.standings if standing.check.id == check_id)


def policy(**record) -> None:
    ProviderInventory.objects.create(
        kind="tailscale.policy", observed_at=timezone.now(), records=[{"record": "policy", **record}]
    )


class DeclarationTests(TestCase):
    def test_each_check_is_declared_once_and_says_what_it_stops(self):
        ids = [check.id for check in STANDARD]

        self.assertEqual(len(ids), len(set(ids)))
        for check in STANDARD:
            with self.subTest(check=check.id):
                self.assertFalse(check.stops.endswith("."), "it finishes 'Stops ...'")
                self.assertTrue(check.holds.endswith("."))
                self.assertTrue(check.step.endswith("."))
                self.assertNotEqual(check.label, check.unmet)

    def test_a_misspelt_setting_refuses_to_start(self):
        env = {**os.environ, "SEVERINO_POSTURE": "advnced", "DJANGO_SETTINGS_MODULE": "hq.config.settings"}
        result = subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            env=env,
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SEVERINO_POSTURE must be "advanced" or unset', result.stderr)


class ReadingTests(TestCase):
    @override_settings(AUTHENTICATION_BACKENDS=PASSWORDS)
    def test_password_sign_in_is_unmet(self):
        self.assertFalse(standing("sign-in").met)

    @override_settings(AUTHENTICATION_BACKENDS=SSO_ONLY)
    def test_single_sign_on_only_is_met(self):
        self.assertTrue(standing("sign-in").met)

    def test_a_check_with_no_reading_is_not_read_rather_than_unmet(self):
        found = standing("tailnet-lock")

        self.assertIsNone(found.reading.met)
        self.assertEqual(found.reading.evidence, "Not read yet")

    def test_an_unread_check_names_the_connection_that_would_read_it(self):
        found = standing("tailnet-lock")

        self.assertEqual(found.reading.remedy, "connect Tailscale")
        self.assertEqual(found.reading.remedy_url, reverse("control_plane:connections") + "#connection-setup-heading")
        self.assertEqual(found.icon, "clock")

    def test_once_connected_an_unread_check_only_waits(self):
        from hq.domains.control_plane.models import ProviderConnection

        ProviderConnection.objects.create(
            controller_id="example-controller",
            connection_ref="example-tailnet",
            provider="tailscale",
            endpoint="https://api.example.com",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        found = standing("tailnet-lock")

        self.assertNotIn("connect", found.reading.remedy)

    def test_an_allow_all_grant_is_unmet(self):
        policy(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"]}])

        self.assertFalse(standing("tailnet-policy").met)

    def test_an_allow_all_acl_is_unmet(self):
        policy(grants=[], document='{"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}')

        self.assertFalse(standing("tailnet-policy").met)

    def test_named_grants_are_met(self):
        policy(grants=[{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:443"]}])

        self.assertTrue(standing("tailnet-policy").met)

    def test_tailnet_lock_is_read_from_the_policy_reading(self):
        policy(grants=[{"src": ["group:admins"]}], lock={"enabled": True, "trusted_keys": 2})

        self.assertTrue(standing("tailnet-lock").met)

    def test_an_unread_check_is_not_counted_as_unmet(self):
        policy(grants=[{"src": ["group:admins"]}], lock={"enabled": True, "trusted_keys": 2})

        found = estate_posture(connection_catalog(principal=cli_principal()))

        self.assertIn(f"{found.unread_count} not read yet", found.count_line)
        self.assertTrue(found.count_line.startswith(f"{found.met_count} of {len(STANDARD)} met"))

    def test_the_count_names_each_check_that_is_not_met(self):
        policy(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"]}], lock={"enabled": False, "trusted_keys": 0})

        found = estate_posture(connection_catalog(principal=cli_principal()))

        for standing in found.unmet:
            if standing.reading.met is False:
                self.assertIn(standing.check.label, found.count_line)
        self.assertTrue(any(standing.reading.met is False for standing in found.unmet))


@override_settings(AUTHENTICATION_BACKENDS=PASSWORDS)
class FindingTests(TestCase):
    def test_without_advanced_posture_nothing_is_raised(self):
        self.assertEqual(raised(RULE), [])

    @override_settings(SEVERINO_POSTURE="advanced")
    def test_under_advanced_posture_each_unmet_check_is_raised(self):
        found = {finding["title"]: finding for finding in raised(RULE)}

        finding = found["Password sign-in is installed"]
        self.assertEqual(finding["severity"], "attention")
        self.assertIn("a guessed or reused password", finding["explanation"])
        self.assertIn("HQ has not read whether tailnet lock is on", found)

    @override_settings(SEVERINO_POSTURE="advanced", AUTHENTICATION_BACKENDS=SSO_ONLY)
    def test_a_met_check_is_not_raised(self):
        self.assertNotIn("Password sign-in is installed", [finding["title"] for finding in raised(RULE)])


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("posture-reader"))

    def test_the_connections_page_shows_each_check_and_no_paragraph_of_them(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertContains(response, "HQ's standard")
        # How to enforce it is the docs', not a call to action on the page.
        self.assertNotContains(response, "SEVERINO_POSTURE=advanced")
        for check in STANDARD:
            self.assertContains(response, check.label)
            # A met check's sentence is its tooltip, never a paragraph over the list.
            self.assertNotContains(response, f"{check.holds} ")

    @override_settings(SEVERINO_POSTURE="advanced")
    def test_the_connection_panel_says_advanced_posture_is_enabled(self):
        response = self.client.get(reverse("connection"))

        self.assertContains(response, "Advanced HQ Posture enabled")
        self.assertNotContains(response, "SEVERINO_POSTURE=advanced")

    def test_the_api_carries_the_standard(self):
        from ..connection_context import connections_context

        standard = connections_context(principal=cli_principal()).as_dict()["standard"]

        self.assertFalse(standard["advanced"])
        self.assertEqual([check["id"] for check in standard["checks"]], [check.id for check in STANDARD])


class ScopeTests(TestCase):
    def reading(self, credential: str, provider: str):
        from types import SimpleNamespace

        from ..posture import Inputs, posture

        connection = SimpleNamespace(
            instance=SimpleNamespace(label="Example", credential_model=credential, kind=provider),
            abilities=(),
        )
        edge = SimpleNamespace(state="good", evidence="")
        found = posture(Inputs(edge, (SimpleNamespace(connections=(connection,)),)))
        return next(standing for standing in found.standings if standing.check.id == "scope").reading

    def test_a_full_account_token_where_a_scoped_one_is_offered_is_unmet(self):
        reading = self.reading("coarse", "cloudflare_api")

        self.assertFalse(reading.met)
        self.assertEqual(reading.evidence, "Full account access: Example")

    def test_a_login_with_no_scoped_form_is_met_but_not_a_clean_pass(self):
        reading = self.reading("coarse", "npm")

        self.assertTrue(reading.met)
        self.assertTrue(reading.qualified)
        self.assertEqual(reading.evidence, "Nothing narrower offered for Example")

    def test_a_scoped_token_is_met(self):
        reading = self.reading("scoped", "cloudflare_api")

        self.assertTrue(reading.met)
        self.assertFalse(reading.qualified)
        self.assertEqual(reading.evidence, "1 scoped token")

    def test_with_no_key_held_there_is_nothing_to_judge(self):
        self.assertIsNone(self.reading("none", "").met)


class ToneTests(TestCase):
    def standing(self, met, enforced):
        from ..posture import Reading, Standing

        return Standing(STANDARD[0], Reading(met, ""), enforced)

    def test_without_advanced_posture_an_unmet_check_is_quiet(self):
        self.assertEqual(self.standing(False, enforced=False).state, "neutral")
        self.assertEqual(self.standing(False, enforced=False).icon, "circle")

    def test_under_advanced_posture_an_unmet_check_is_a_fault(self):
        self.assertEqual(self.standing(False, enforced=True).state, "attention")

    def test_an_unread_check_is_never_a_fault(self):
        self.assertEqual(self.standing(None, enforced=True).state, "neutral")
        self.assertEqual(self.standing(None, enforced=True).icon, "clock")

    def test_a_qualified_pass_is_ticked_but_not_green(self):
        from ..posture import Reading, Standing

        found = Standing(STANDARD[0], Reading(True, "", qualified=True))

        self.assertEqual((found.state, found.icon), ("neutral", "check-circle"))
