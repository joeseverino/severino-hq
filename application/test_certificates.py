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
