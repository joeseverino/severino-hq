"""HQ's standard: each check read from what decides it, shown always, raised
under Advanced HQ posture (``standards.Standard``, held in test_standards)."""

import os
import subprocess
import sys

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
from hq.platform.application.markup_testing import links, text
from hq.platform.core.testing import SignedInTestCase

from ..connection_catalog import connection_catalog
from ..inventory_testing import store
from ..posture import STANDARD, estate_posture
from ..report_testing import bridged
from ..security import cli_principal
from ..standards import CheckResult, Reading, Standing
from .test_tailnet_posture import tailnet_connection

PASSWORDS = ["hq.platform.core.oidc.HQOIDCAuthenticationBackend", "django.contrib.auth.backends.ModelBackend"]
SSO_ONLY = ["hq.platform.core.oidc.HQOIDCAuthenticationBackend"]


def standing(check_id: str) -> CheckResult:
    found = estate_posture(connection_catalog(principal=cli_principal()))
    return next(result for result in found.results if result.check.id == check_id)


TAILNET_LOCK = "host.tailnet_lock"
# The record the controller sends for `tailscale lock status --json`.
A_LOCK = {
    "record": "lock",
    "enabled": True,
    "node_key_signed": True,
    "signers": [{"key": "tlpub:01"}, {"key": "tlpub:02"}],
    "locked_out": [],
}
LAUNCHER_REFUSED = "the launcher could not ask the tailnet daemon for its lock status"


def sweep(payload) -> None:
    bridged("inventory", payload, controller_id="example-controller")


def policy(*, lock=None, **record) -> None:
    ProviderInventory.objects.create(
        kind="tailscale.policy", observed_at=timezone.now(), records=[{"record": "policy", **record}]
    )
    if lock is not None:
        store("host.tailnet_lock", {"record": "lock", **lock})


class DeclarationTests(TestCase):
    def test_each_check_is_declared_once_and_says_what_it_stops(self):
        ids = [check.id for check in STANDARD]

        self.assertEqual(len(ids), len(set(ids)))
        for check in STANDARD:
            with self.subTest(check=check.id):
                self.assertFalse(check.why.endswith("."), "it finishes 'Stops ...'")
                self.assertTrue(check.holds.endswith("."))
                self.assertTrue(check.fix.endswith("."))
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

        self.assertEqual(found.state, Standing.UNAVAILABLE)
        self.assertEqual(found.icon, "clock")

    def test_an_unread_check_names_the_connection_that_would_read_it(self):
        found = standing("tailnet-policy")

        self.assertEqual(found.reading.evidence, "No Tailscale connection reads it")
        self.assertEqual(found.reading.fix, "connect Tailscale")
        self.assertEqual(found.reading.fix_url, reverse("control_plane:connections") + "#connection-setup-heading")
        self.assertEqual(found.icon, "clock")

    def test_once_connected_an_unread_check_only_waits(self):

        ProviderConnection.objects.create(
            controller_id="example-controller",
            connection_ref="example-tailnet",
            provider="tailscale",
            endpoint="https://api.example.com",
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )

        found = standing("tailnet-policy")

        self.assertNotIn("connect", found.reading.fix)

    def test_an_allow_all_grant_is_unmet(self):
        policy(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"]}])

        self.assertFalse(standing("tailnet-policy").met)

    def test_an_allow_all_acl_is_unmet(self):
        policy(grants=[], document='{"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}')

        self.assertFalse(standing("tailnet-policy").met)

    def test_named_grants_are_met(self):
        policy(grants=[{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:443"]}])

        self.assertTrue(standing("tailnet-policy").met)

    def test_a_swept_lock_reading_meets_the_check(self):
        sweep({TAILNET_LOCK: {"ok": True, "records": [A_LOCK]}})

        found = standing("tailnet-lock")
        self.assertTrue(found.met)
        self.assertEqual(found.reading.evidence, "2 signing keys")

    def test_a_policy_read_after_the_lock_leaves_it_read(self):
        sweep({TAILNET_LOCK: {"ok": True, "records": [A_LOCK]}})
        sweep({"tailscale.policy": {"ok": True, "records": [{"record": "policy", "document": "{}"}]}})

        self.assertTrue(standing("tailnet-lock").met)

    def test_a_lock_read_that_failed_keeps_the_last_one(self):
        sweep({TAILNET_LOCK: {"ok": True, "records": [A_LOCK]}})
        sweep({TAILNET_LOCK: {"ok": False, "records": [], "error": LAUNCHER_REFUSED}})

        self.assertTrue(standing("tailnet-lock").met)

    def test_a_lock_never_read_says_why(self):
        sweep({TAILNET_LOCK: {"ok": False, "records": [], "error": LAUNCHER_REFUSED}})

        found = standing("tailnet-lock")
        self.assertEqual(found.state, Standing.UNAVAILABLE)
        self.assertEqual(found.reading.evidence, LAUNCHER_REFUSED)

    # No heartbeat: only the readings say whether a controller has run.
    @override_settings(SEVERINO_CONTROLLER_HEARTBEAT="/nonexistent/example-heartbeat")
    def test_an_unread_check_says_which_cause_holds_it(self):
        tailnet_connection()
        cases = (
            ("no controller has run", lambda: None, "HQ has not read it"),
            (
                "no read has arrived",
                lambda: store("example.other"),
                "Waiting on the first read of",
            ),
            (
                "the last read failed",
                lambda: store("tailscale.policy", reachable=False, error="Tailscale refused the token."),
                "Tailscale refused the token",
            ),
            (
                "the reading held no answer",
                lambda: store("tailscale.policy", {"record": "example"}),
                "holds nothing to judge it by",
            ),
        )
        for cause, arrange, said in cases:
            with self.subTest(cause=cause):
                ProviderInventory.objects.all().delete()
                arrange()

                found = standing("tailnet-policy").reading

                self.assertEqual(found.standing, Standing.UNAVAILABLE)
                self.assertIn(said, found.evidence)
                self.assertFalse(found.evidence.endswith("."), "the panel ends the sentence")

    def test_no_unread_check_lacks_its_cause(self):
        for estate, arrange in (
            ("nothing connected", lambda: None),
            ("connected, no controller", tailnet_connection),
            ("connected and read", lambda: (tailnet_connection(), store("example.other"))),
        ):
            with self.subTest(estate=estate):
                ProviderConnection.objects.all().delete()
                arrange()
                unread = [
                    found
                    for found in estate_posture(connection_catalog(principal=cli_principal())).results
                    if found.state == Standing.UNAVAILABLE
                ]

                self.assertTrue(unread)
                for found in unread:
                    self.assertTrue(found.reading.evidence, found.check.id)
                    self.assertNotEqual(found.reading.evidence.lower(), "not read yet", found.check.id)

    def test_an_unread_check_is_not_counted_as_unmet(self):
        policy(
            grants=[{"src": ["group:admins"]}],
            lock={"enabled": True, "signers": [{"key": "tlpub:01"}, {"key": "tlpub:02"}]},
        )

        found = estate_posture(connection_catalog(principal=cli_principal()))

        self.assertIn(f"{found.unread} not read yet", found.count_line)
        self.assertTrue(found.count_line.startswith(f"{found.met} of {len(STANDARD)} met"))

    def test_the_count_names_each_check_that_is_not_met(self):
        policy(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"]}], lock={"enabled": False, "signers": []})

        found = estate_posture(connection_catalog(principal=cli_principal()))

        self.assertTrue(found.unmet)
        for result in found.unmet:
            self.assertIn(result.check.label, found.count_line)


class PageTests(SignedInTestCase):
    def test_the_connections_page_shows_each_check_and_no_paragraph_of_them(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertIn("HQ's standard", text(response))
        # How to enforce it is the docs', not a call to action on the page.
        self.assertNotContains(response, "SEVERINO_POSTURE=advanced")
        said = text(response)
        for check in STANDARD:
            self.assertIn(check.label, said)
            # A met check's sentence is its tooltip, never a paragraph over the list.
            self.assertNotContains(response, f"{check.holds} ")

    @override_settings(SEVERINO_POSTURE="advanced")
    def test_the_connection_panel_says_advanced_posture_is_enabled(self):
        response = self.client.get(reverse("connection"))

        self.assertIn("Advanced HQ posture", [words for words, _target in links(response)])
        self.assertNotContains(response, "SEVERINO_POSTURE=advanced")

    def test_the_api_carries_the_standard_and_what_would_read_a_check_not_read(self):
        from ..connection_context import connections_context

        standard = connections_context(principal=cli_principal()).as_dict()["standard"]

        self.assertFalse(standard["advanced"])
        self.assertEqual([check["id"] for check in standard["checks"]], [check.id for check in STANDARD])
        served = next(check for check in standard["checks"] if check["id"] == "tailnet-policy")
        self.assertEqual((served["state"], served["read_with"]), (Standing.UNAVAILABLE, "connect Tailscale"))
        self.assertTrue(served["read_with_url"])


class ToneTests(TestCase):
    def result(self, reading, *, enforced=True):
        return CheckResult(STANDARD[0], reading, enforced=enforced)

    def test_a_limited_pass_is_ticked_but_not_green(self):
        found = self.result(Reading.met(limited=True))

        self.assertEqual((found.tone, found.icon), ("neutral", "check-circle"))
