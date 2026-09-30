"""Tests for certificates: the cPanel site plan, ACME ownership, verification and what a renewal records."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from control_plane.provider_adapters import onepassword

from controller_runtime import provider_http, tls, tls_issuance, tls_verification
from control_plane.provider_adapters.contracts import ProviderError, ProviderResult
from .test_support import A_RECORDED_CERTIFICATE, AN_OBSERVATION

# One account, three sites; the first carries its aliases, as shared hosting
# serves `www.` and a parked domain from the main site.
_ACCOUNT_SITES = {
    "sites": {
        "example.test": ["example.test", "www.example.test", "parked.example"],
        "shop.example.test": ["shop.example.test", "www.shop.example.test"],
        "lab.example.test": ["lab.example.test"],
    }
}


class CPanelSitePlanTests(TestCase):
    """Which cPanel sites a certificate goes to, decided before anything is issued.

    A target must never install on one site and be checked at names another site
    serves: the install would succeed and the check could never pass.
    """

    def _consumer(self, verify, install=()):
        return {
            "kind": "cpanel",
            "name": "shared-hosting",
            "connection_ref": "example-cpanel",
            "verify_domains": list(verify),
            "install_domains": list(install),
        }

    def _plan(self, consumer, answer=None):
        with mock.patch("controller_runtime.commands.run_ssh") as ssh:
            ssh.return_value = json.dumps(
                _ACCOUNT_SITES if answer is None else answer
            ).encode()
            sites = tls._cpanel_sites(consumer)
        ssh.assert_called_once_with("example-cpanel", "sites")
        return sites

    def test_without_a_list_it_installs_on_every_site_serving_a_checked_name(self):
        consumer = self._consumer(["www.example.test", "parked.example", "shop.example.test"])

        self.assertEqual(self._plan(consumer), ["example.test", "shop.example.test"])

    def test_a_declared_name_installs_on_the_whole_site_that_serves_it(self):
        consumer = self._consumer(["www.example.test"], install=["parked.example"])

        self.assertEqual(self._plan(consumer), ["example.test"])

    def test_a_checked_name_on_a_site_it_does_not_install_on_is_refused(self):
        consumer = self._consumer(
            ["example.test", "shop.example.test"], install=["shop.example.test"]
        )

        with self.assertRaisesRegex(
            ProviderError,
            "checked at example.test but installs only on shop.example.test",
        ):
            self._plan(consumer)

    def test_a_name_the_account_does_not_serve_is_refused(self):
        consumer = self._consumer(["example.test", "elsewhere.example"])

        with self.assertRaisesRegex(
            ProviderError, "does not serve elsewhere.example"
        ):
            self._plan(consumer)

    def test_an_unreadable_or_empty_answer_is_refused(self):
        consumer = self._consumer(["example.test"])
        for answer in ({"sites": {}}, {"no": "sites"}, []):
            with self.subTest(answer=answer):
                with self.assertRaises(ProviderError):
                    self._plan(consumer, answer)
        with mock.patch("controller_runtime.commands.run_ssh", return_value=b"not json"):
            with self.assertRaisesRegex(ProviderError, "could not read"):
                tls._cpanel_sites(consumer)

    @mock.patch("controller_runtime.tls_issuance.issue_certificate")
    @mock.patch("controller_runtime.commands.run_ssh")
    def test_an_unsatisfiable_target_is_refused_before_the_ca_is_asked(self, ssh, issue):
        ssh.return_value = json.dumps(_ACCOUNT_SITES).encode()
        spec = {
            "domains": ["example.test", "*.example.test"],
            "consumers": [
                {"kind": "caddy", "name": "edge", "connection_ref": "example-edge"},
                self._consumer(
                    ["example.test", "shop.example.test"], install=["shop.example.test"]
                ),
            ],
        }

        with self.assertRaisesRegex(ProviderError, "installs only on"):
            tls.renew_tls(spec)

        issue.assert_not_called()
        # Nothing was read from the rollback source either: the plan comes first.
        ssh.assert_called_once_with("example-cpanel", "sites")

    @mock.patch("controller_runtime.commands.run_ssh")
    def test_one_login_installs_on_every_planned_site(self, ssh):
        consumer = self._consumer(["example.test"])
        fullchain = b"-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\nchain\n"

        status = tls._deploy_certificate(
            {"domains": ["example.test"], "consumers": [consumer]},
            fullchain,
            b"key",
            {"shared-hosting": ["example.test", "shop.example.test"]},
        )

        ssh.assert_called_once()
        connection_ref, operation, payload = ssh.call_args.args
        self.assertEqual((connection_ref, operation), ("example-cpanel", "deploy"))
        sent = json.loads(payload)
        self.assertEqual(sent["sites"], ["example.test", "shop.example.test"])
        self.assertIn("leaf", sent["cert"])
        self.assertEqual(sent["cabundle"], "chain\n")
        self.assertEqual(
            status["cpanel_sites"], {"shared-hosting": ["example.test", "shop.example.test"]}
        )

    @mock.patch("controller_runtime.tls_verification._tls_verification_policy", return_value=(30, 5))
    @mock.patch("time.monotonic", side_effect=[0, 31])
    @mock.patch("controller_runtime.tls_verification.reconcile_tls")
    def test_a_consumer_that_never_activates_is_named_with_its_names(
        self, reconcile, _clock, _policy
    ):
        reconcile.return_value = ProviderResult(
            changed=False,
            status={
                "consumers": [
                    {"consumer": "edge", "consumer_kind": "caddy", "domain": "a.example.test", "fingerprint_sha256": "new"},
                    {"consumer": "shared-hosting", "consumer_kind": "cpanel", "domain": "www.example.test", "fingerprint_sha256": "old"},
                    {"consumer": "shared-hosting", "consumer_kind": "cpanel", "domain": "example.test", "fingerprint_sha256": "old"},
                ]
            },
            conditions=[],
            message="observed",
        )

        with self.assertRaises(ProviderError) as caught:
            tls_verification.verify_tls_deployment({"consumers": [{}, {}]}, "new")

        self.assertEqual(
            str(caught.exception),
            "1 of 2 TLS consumers did not activate the certificate within 30s: "
            "shared-hosting still serves the previous certificate at "
            "example.test, www.example.test.",
        )


class AcmeOwnershipTests(TestCase):
    """Certbot copies the previous key's owner onto the new one on every renewal.

    A process that is not root cannot chown to a group it is not in, so one file
    carrying another group fails the save: after the CA has issued. The check
    runs first, so that costs nothing.
    """

    def test_a_tree_wholly_the_processes_own_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config", "archive").mkdir(parents=True)
            Path(directory, "config", "archive", "privkey1.pem").write_text("k")

            self.assertEqual(tls_issuance._foreign_acme_entry(Path(directory)), "")

    def test_an_entry_with_another_group_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config").mkdir()
            Path(directory, "config", "privkey1.pem").write_text("k")
            real_gid = os.getgid()
            with mock.patch("os.getgid", return_value=real_gid + 1):
                found = tls_issuance._foreign_acme_entry(Path(directory))

        self.assertIn("config", found)
        self.assertIn(f"not {os.getuid()}:{real_gid + 1}", found)

    @mock.patch("controller_runtime.commands.run_command")
    @mock.patch("controller_runtime.tls_issuance._foreign_acme_entry", return_value="config/x is owned 1:2, not 3:4")
    def test_issuance_stops_before_certbot_when_the_tree_is_not_its_own(self, _foreign, run):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict("os.environ", {"HQ_ACME_DIR": directory}):
                with self.assertRaisesRegex(
                    ProviderError, "config/x is owned 1:2.*nothing was requested"
                ):
                    tls_issuance.issue_certificate({"domains": ["example.test"]})

        run.assert_not_called()


class PrivateWriteTests(TestCase):
    def test_a_credential_is_private_from_creation_and_never_written_through_a_link(self):
        with tempfile.TemporaryDirectory() as directory:
            elsewhere = Path(directory, "elsewhere")
            elsewhere.write_text("kept")
            credentials = Path(directory, "cloudflare.ini")
            credentials.symlink_to(elsewhere)
            previous = os.umask(0)
            try:
                tls_issuance._write_private(credentials, "secret\n")
            finally:
                os.umask(previous)

            self.assertEqual(elsewhere.read_text(), "kept")
            self.assertFalse(credentials.is_symlink())
            self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
            self.assertEqual(credentials.read_text(), "secret\n")


class TlsReadingTests(TestCase):
    """Every consumer's outcome lands in one reading, pinned field by field."""

    def reading(self, days, fingerprint, domain):
        when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        return {
            "domain": domain,
            "not_after": when.replace(microsecond=0).isoformat(),
            "fingerprint_sha256": fingerprint,
            "issuer": f"Issuer {fingerprint}",
            "sans": [domain],
            "certificate_pem": f"PEM {fingerprint}",
        }

    def reconcile(self, spec, readings, endpoints):
        def observe(domain, *, connect_host=None):
            found = readings[domain]
            if isinstance(found, Exception):
                raise found
            return dict(found)

        def endpoint(consumer):
            found = endpoints.get(consumer["name"])
            if isinstance(found, Exception):
                raise found
            return found

        covered = [{"domain_names": ["a.example.com", "b.example.com"]}]
        with (
            mock.patch.object(tls_verification, "_observe_tls_domain", side_effect=observe) as seen,
            mock.patch.object(tls_verification, "_consumer_tls_endpoint", side_effect=endpoint),
            mock.patch.object(tls_verification, "_npm_covered_hosts", return_value=covered) as npm,
        ):
            result = tls_verification.reconcile_tls(spec)
        return result, seen, npm

    def test_every_outcome_is_reported_against_its_consumer(self):
        far = self.reading(300, "one", "a.example.com")
        near = self.reading(200, "two", "b.example.com")
        spec = {
            "domains": ["*.example.com"],
            "renewal_window_days": 250,
            "consumers": [
                {"kind": "npm", "name": "proxy", "discover_covered_hosts": True,
                 "verify_domains": ["b.example.com"]},
                {"kind": "caddy", "name": "silent"},
                {"kind": "caddy", "name": "lost", "verify_domains": ["c.example.com"]},
                {"kind": "other", "name": "down", "verify_domains": ["d.example.com"]},
            ],
        }
        result, seen, npm = self.reconcile(
            spec,
            {
                "a.example.com": far,
                "b.example.com": near,
                "d.example.com": ProviderError("d refused"),
            },
            {"proxy": "192.0.2.5", "lost": ProviderError("no endpoint"), "down": None},
        )

        npm.assert_called_once_with(["*.example.com"])
        self.assertEqual(
            seen.call_args_list,
            [
                mock.call("a.example.com", connect_host="192.0.2.5"),
                mock.call("b.example.com", connect_host="192.0.2.5"),
                mock.call("d.example.com", connect_host=None),
            ],
        )
        self.assertFalse(result.changed)
        self.assertEqual(result.message, "TLS consumers observed.")
        public = [
            {**{k: v for k, v in far.items() if k != "certificate_pem"},
             "consumer": "proxy", "consumer_kind": "npm"},
            {**{k: v for k, v in near.items() if k != "certificate_pem"},
             "consumer": "proxy", "consumer_kind": "npm"},
        ]
        self.assertEqual(
            result.status,
            {
                "issuer": "Issuer one",
                "not_after": near["not_after"],
                "artifact_not_after": far["not_after"],
                "certificate_pem": "PEM one",
                "verified_domains": ["a.example.com", "b.example.com"],
                "consumers": public,
                "unreachable_consumers": [
                    {"consumer": "lost", "domain": "", "endpoint": "", "port": "443",
                     "reason": "no endpoint"},
                    {"consumer": "down", "domain": "d.example.com",
                     "endpoint": "d.example.com", "port": "443", "reason": "d refused"},
                ],
            },
        )
        self.assertEqual(
            [(c["type"], c["status"], c["reason"]) for c in result.conditions],
            [
                ("Drifted", True, "ConsumerMismatch"),
                ("Degraded", True, "ExpiringSoon"),
                ("Degraded", True, "ConsumerUnverified"),
                ("Degraded", True, "ConsumerUnreachable"),
            ],
        )
        self.assertRegex(result.conditions[1]["message"], r"expires in (199|200) days\.$")
        self.assertEqual(
            result.conditions[2]["message"],
            "No verification domain is declared for: silent",
        )
        self.assertEqual(
            result.conditions[3]["message"], "Could not be read: lost, d.example.com"
        )

    def test_one_current_certificate_everywhere_is_verified(self):
        current = self.reading(300, "one", "a.example.com")
        result, _, npm = self.reconcile(
            {
                "renewal_window_days": 30,
                "consumers": [{"kind": "npm", "name": "proxy",
                               "verify_domains": ["a.example.com"]}],
            },
            {"a.example.com": current},
            {"proxy": "192.0.2.5"},
        )

        npm.assert_not_called()
        self.assertEqual(
            result.conditions,
            [{"type": "Ready", "status": True, "reason": "Verified",
              "message": "All TLS consumers are current."}],
        )

    def test_nothing_to_verify_is_a_failure_that_says_so(self):
        with self.assertRaisesRegex(
            ProviderError, "No TLS verification domains were declared"
        ):
            self.reconcile(
                {"renewal_window_days": 30, "consumers": [{"kind": "npm", "name": "p"}]},
                {},
                {},
            )


class RecordingTheFactsIsNeverTheCertificatesJobTests(TestCase):
    """A password manager being unreachable is not a certificate being wrong.

    Raising here would fail the reconcile, which marks the resource degraded and
    queues an automatic retry of a deployment that had nothing wrong with it,
    over a note that did not get filed.
    """

    def _reconcile_with_a_failing_publisher(self, failure):
        observed = ProviderResult(
            changed=False,
            status=dict(AN_OBSERVATION),
            conditions=[provider_http.condition("Ready", True, "Verified", "Current.")],
            message="TLS consumers observed.",
        )
        with (
            mock.patch.object(tls, "apply_tls_reconcile", return_value=observed),
            mock.patch.object(onepassword, "publish", side_effect=failure),
        ):
            return tls._tls_reconcile(A_RECORDED_CERTIFICATE, apply=True)

    def test_a_failed_publication_leaves_the_reconcile_successful(self):
        result = self._reconcile_with_a_failing_publisher(
            ProviderError("1Password write for a certificate failed.")
        )

        self.assertEqual(
            [condition["type"] for condition in result.conditions], ["Ready"]
        )
        self.assertTrue(result.conditions[0]["status"])

    def test_a_failed_publication_is_reported_rather_than_passed_over(self):
        result = self._reconcile_with_a_failing_publisher(
            ProviderError("1Password write for a certificate failed.")
        )
        published = result.status["published_facts"][0]

        self.assertFalse(published["written"])
        self.assertEqual(published["target"], "an-example-certificate-onepassword")
        self.assertIn("Facts were not recorded on", result.message)

    def test_a_tool_that_is_not_installed_degrades_the_same_way(self):
        """`op` absent raises through the subprocess boundary, not the provider's."""

        result = self._reconcile_with_a_failing_publisher(OSError("no such file"))

        self.assertEqual(
            [condition["type"] for condition in result.conditions], ["Ready"]
        )
        self.assertFalse(result.status["published_facts"][0]["written"])

    def test_a_certificate_recording_nowhere_reports_no_publication_at_all(self):
        """Every certificate that names no vault must be untouched by this."""

        observed = ProviderResult(
            changed=False,
            status={"issuer": "An Example Authority"},
            conditions=[],
            message=".",
        )
        spec = {**A_RECORDED_CERTIFICATE, "publish_to": []}
        with mock.patch.object(tls, "apply_tls_reconcile", return_value=observed):
            result = tls._tls_reconcile(spec, apply=True)

        self.assertNotIn("published_facts", result.status)
