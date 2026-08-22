"""Holding a certificate an operator generated elsewhere.

This is the one secret HQ keeps on purpose, against a codebase that otherwise
refuses to hold any -- so the guarantees are worth asserting rather than
assuming: encrypted at rest, refused outright when there is nowhere safe to put
it, never in a serializer, and checked before it is trusted.
"""

from __future__ import annotations

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import TestCase, override_settings

from control_plane.models import CertificateMaterial, ManagedResource
from core import secrets

from .certificates import (
    CertificateError,
    UploadCertificateCommand,
    inspect,
    material_for,
    store_certificate,
)
from .infrastructure import serialize_resource
from .security import cli_principal

A_KEY = "test-only-secret-store-key-long-enough"


def a_certificate(name: str = "newhost.example.test", *, before=-1, after=825):
    """A self-signed pair, shaped like what cert-gen produces."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=before))
        .not_valid_after(now + datetime.timedelta(days=after))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


class InspectionTests(TestCase):
    def test_the_names_and_expiry_are_read_out_of_the_certificate(self):
        fullchain, private_key = a_certificate()

        details = inspect(fullchain, private_key)

        self.assertEqual(details["domains"], ["newhost.example.test"])
        self.assertGreater(details["not_after"], datetime.datetime.now(datetime.timezone.utc))

    def test_a_key_that_belongs_to_a_different_certificate_is_refused(self):
        """The failure this exists to catch.

        A mismatched pair is accepted by every editor and every clipboard, and
        fails when a browser refuses the handshake -- after deployment, on a
        service that was working a minute earlier.
        """
        fullchain, _ = a_certificate("one.example.test")
        _, other_key = a_certificate("two.example.test")

        with self.assertRaisesRegex(CertificateError, "does not belong"):
            inspect(fullchain, other_key)

    def test_an_expired_certificate_is_refused(self):
        fullchain, private_key = a_certificate(before=-60, after=-2)

        with self.assertRaisesRegex(CertificateError, "expired"):
            inspect(fullchain, private_key)

    def test_something_that_is_not_a_certificate_says_which_file_to_paste(self):
        with self.assertRaisesRegex(CertificateError, "fullchain.pem"):
            inspect("not a certificate", "not a key")


@override_settings(SEVERINO_SECRET_STORE_KEY=A_KEY)
class StorageTests(TestCase):
    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="newhost-cert",
            kind="tls.uploaded_certificate",
            spec={
                "certificate_name": "newhost.example.test",
                "install_on": ["homelab-npm"],
            },
        )
        self.fullchain, self.private_key = a_certificate()

    def _store(self):
        return store_certificate(
            UploadCertificateCommand(
                key=self.resource.key,
                fullchain=self.fullchain,
                private_key=self.private_key,
            ),
            principal=cli_principal(),
        )

    def test_the_key_is_encrypted_at_rest(self):
        self._store()

        material = CertificateMaterial.objects.get()
        self.assertNotIn("PRIVATE KEY", material.sealed_private_key)
        self.assertNotIn("BEGIN", material.sealed_fullchain)

    def test_the_controller_gets_the_original_material_back(self):
        self._store()

        recovered = material_for(self.resource.key)

        self.assertEqual(recovered["private_key"], self.private_key)
        self.assertEqual(recovered["fullchain"], self.fullchain)
        self.assertEqual(recovered["domains"], ["newhost.example.test"])

    def test_what_the_certificate_covers_is_recorded_for_reading(self):
        """Expiry and names are printed, not protected.

        Anyone who can reach the service already sees them, and an operator
        needs to know when to generate the next one.
        """
        self._store()

        material = CertificateMaterial.objects.get()
        self.assertEqual(material.domains, ["newhost.example.test"])
        self.assertTrue(material.fingerprint_sha256)
        self.assertTrue(material.not_after)

    def test_no_serializer_ever_emits_it(self):
        self._store()

        serialized = str(serialize_resource(self.resource)).upper()

        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("BEGIN CERTIFICATE", serialized)

    def test_the_response_does_not_hand_the_secret_back(self):
        """A response body is the easiest place for a secret to end up in a log."""
        result = self._store()

        self.assertNotIn("private_key", result)
        self.assertNotIn("fullchain", result)

    def test_uploading_again_replaces_rather_than_accumulates(self):
        self._store()
        self.fullchain, self.private_key = a_certificate()
        self._store()

        self.assertEqual(CertificateMaterial.objects.count(), 1)
        self.assertEqual(
            material_for(self.resource.key)["private_key"], self.private_key
        )

    def test_a_rejected_pair_stores_nothing(self):
        _, other_key = a_certificate("other.example.test")

        with self.assertRaises(CertificateError):
            store_certificate(
                UploadCertificateCommand(
                    key=self.resource.key,
                    fullchain=self.fullchain,
                    private_key=other_key,
                ),
                principal=cli_principal(),
            )

        self.assertFalse(CertificateMaterial.objects.exists())


class FailClosedTests(TestCase):
    """No key configured means no storage, not quiet plaintext."""

    @override_settings(SEVERINO_SECRET_STORE_KEY="")
    def test_storing_is_refused_when_there_is_nowhere_safe_to_put_it(self):
        resource = ManagedResource.objects.create(
            key="newhost-cert",
            kind="tls.uploaded_certificate",
            spec={"certificate_name": "n", "install_on": ["homelab-npm"]},
        )
        fullchain, private_key = a_certificate()

        with self.assertRaises(secrets.SecretsUnavailable):
            store_certificate(
                UploadCertificateCommand(
                    key=resource.key, fullchain=fullchain, private_key=private_key
                ),
                principal=cli_principal(),
            )

        self.assertFalse(CertificateMaterial.objects.exists())

    @override_settings(SEVERINO_SECRET_STORE_KEY="far-too-short")
    def test_a_weak_key_is_refused_rather_than_stretched(self):
        with self.assertRaises(secrets.SecretsUnavailable):
            secrets.seal("anything")

    @override_settings(SEVERINO_SECRET_STORE_KEY=A_KEY)
    def test_a_rotated_key_reports_rather_than_returning_an_empty_secret(self):
        sealed = secrets.seal("material")

        with override_settings(SEVERINO_SECRET_STORE_KEY="a-different-key-of-full-length"):
            with self.assertRaises(secrets.SecretsUnavailable):
                secrets.unseal(sealed)


@override_settings(SEVERINO_SECRET_STORE_KEY=A_KEY)
class CoverageTests(TestCase):
    """A certificate HQ was given covers the names it carries.

    It covered nothing at all, so every private name it answered for read as
    "no declared certificate covers it" -- permanently, on the service page and
    in the attention queue. That is the whole reason this kind exists: for a
    name no public authority will issue for, an internally signed certificate is
    the only possible answer.
    """

    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="homelab-wildcard",
            kind="tls.uploaded_certificate",
            spec={
                "certificate_name": "homelab-wildcard",
                "install_on": ["a-proxy"],
            },
        )

    def upload(self, name="grafana.example"):
        fullchain, private_key = a_certificate(name)
        store_certificate(
            UploadCertificateCommand(
                key=self.resource.key, fullchain=fullchain, private_key=private_key
            ),
            principal=cli_principal(),
        )
        self.resource.refresh_from_db()

    def covered(self):
        from control_plane.providers import PROVIDERS

        from .infrastructure import resolved_spec

        provider = PROVIDERS[self.resource.kind]
        return tuple(provider.hostnames(resolved_spec(self.resource)))

    def test_it_covers_what_the_uploaded_certificate_carries(self):
        self.upload("grafana.example")

        self.assertEqual(self.covered(), ("grafana.example",))

    def test_it_covers_them_before_the_controller_has_ever_run(self):
        """HQ read the certificate, so it does not need to be told twice.

        Waiting for the first pass left the name uncovered at the one moment an
        operator is certain to look: right after uploading a certificate for it.
        """

        self.upload("grafana.example")

        self.assertEqual(self.resource.observed_generation, 0)
        self.assertEqual(self.covered(), ("grafana.example",))

    def test_one_HQ_has_not_been_given_yet_covers_nothing(self):
        self.assertEqual(self.covered(), ())

    def test_the_service_it_serves_stops_asking_for_a_certificate(self):
        from control_plane.models import ManagedResource as Resource

        from .services import service_or_prospect

        self.upload("grafana.example")
        Resource.objects.create(
            key="grafana-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["grafana.example"],
                "forward_scheme": "http",
                "forward_host": "10.0.0.10",
                "forward_port": 3000,
            },
        )

        service = service_or_prospect("grafana.example")

        self.assertNotIn(
            "no declared certificate covers it", " ".join(service.faults)
        )

    def test_uploading_asks_for_it_to_be_installed(self):
        """The page promises a pass; something has to give that pass a reason."""

        self.resource.observed_generation = self.resource.generation
        self.resource.save(update_fields=["observed_generation"])

        self.upload("grafana.example")

        self.assertGreater(
            self.resource.generation, self.resource.observed_generation
        )

    def test_replacing_it_with_a_renewal_asks_again(self):
        """A renewal covers the same names, and still has to be installed."""

        self.upload("grafana.example")
        self.resource.observed_generation = self.resource.generation
        self.resource.save(update_fields=["observed_generation"])

        self.upload("grafana.example")

        self.assertGreater(
            self.resource.generation, self.resource.observed_generation
        )

    def test_a_proxy_can_be_pointed_at_one_from_the_form(self):
        """The only possible answer for a private name was not on the menu."""

        from control_plane.providers import NameContext

        from .provider_choices import proxy_choices

        self.upload("grafana.example")
        offered = dict(
            proxy_choices(NameContext(hostname="grafana.example"))[
                "certificate_resource"
            ]
        )

        self.assertIn("homelab-wildcard", offered)
