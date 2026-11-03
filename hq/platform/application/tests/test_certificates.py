"""Holding a certificate an operator generated elsewhere.

This is the one secret HQ keeps on purpose, against a codebase that otherwise
refuses to hold any, so the guarantees are worth asserting rather than
assuming: sealed to the controller at rest, refused outright when there is no
key to seal to, never in a serializer, and checked before it is trusted.
"""

import base64
import datetime
import secrets as random

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase, TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, CertificateMaterial, ManagedResource
from hq.platform.application.tests.writers import keyed
from hq.platform.core import secrets
from hq.platform.core.models import AuditLog
from hq.platform.core.tests.test_seal import SUITE, ciphertext, private_key, public_bytes

from ..capabilities import execute_capability
from ..certificates import (
    UPLOAD,
    UploadCertificateCommand,
    inspect,
    material_for,
    store_certificate,
)
from ..infrastructure import serialize_resource
from ..record_errors import CertificateError, PolicyError
from ..security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal, internal_principal, mcp_principal
from .markup import all_elements

# A controller's seed for these tests, and the public half HQ seals to.
SEAL_SEED = random.token_bytes(32)
SEAL_PUBLIC_KEY = base64.b64encode(public_bytes(SEAL_SEED)).decode()
# The slot an uploaded certificate's key is sealed in.
KEY_SLOT = "certificate:private_key"


def opened(sealed: str, resource_key: str, slot: str = KEY_SLOT) -> str:
    """What the controller holding SEAL_SEED reads from a sealed value."""

    info = f"hq-seal/v1|{resource_key}|{slot}".encode()
    return SUITE.decrypt(ciphertext(sealed), private_key(SEAL_SEED), info).decode()


def a_certificate(name: str = "newhost.example.test", *, before=-1, after=825):
    """A self-signed pair, shaped like what cert-gen produces."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=before))
        .not_valid_after(now + datetime.timedelta(days=after))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
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
        self.assertGreater(details["not_after"], datetime.datetime.now(datetime.UTC))

    def test_a_key_that_belongs_to_a_different_certificate_is_refused(self):
        """The failure this exists to catch.

        A mismatched pair is accepted by every editor and every clipboard, and
        fails when a browser refuses the handshake: after deployment, on a
        service that was working a minute earlier.
        """
        fullchain, _ = a_certificate("one.example.test")
        _, other_key = a_certificate("two.example.test")

        with self.assertRaisesRegex(CertificateError, "does not match the certificate"):
            inspect(fullchain, other_key)

    def test_an_expired_certificate_is_refused(self):
        fullchain, private_key = a_certificate(before=-60, after=-2)

        with self.assertRaisesRegex(CertificateError, "expired"):
            inspect(fullchain, private_key)

    def test_something_that_is_not_a_certificate_says_which_file_to_paste(self):
        with self.assertRaisesRegex(CertificateError, "fullchain.pem"):
            inspect("not a certificate", "not a key")


@override_settings(SEVERINO_SEAL_PUBLIC_KEY=SEAL_PUBLIC_KEY)
class StorageTests(TestCase):
    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="newhost-cert",
            kind="tls.uploaded_certificate",
            spec={
                "certificate_name": "newhost.example.test",
                "install_on": ["example-npm"],
            },
        )
        self.fullchain, self.private_key = a_certificate()

    def _store(self):
        return store_certificate(
            UploadCertificateCommand(
                fullchain=self.fullchain,
                private_key=self.private_key,
            ),
            principal=cli_principal(),
            current_key=self.resource.key,
        )

    def test_the_key_is_sealed_to_the_controller_at_rest(self):
        self._store()

        (secret,) = self.resource.sealed_secrets.all()
        self.assertEqual(secret.slot, KEY_SLOT)
        self.assertTrue(secret.sealed.startswith("hq2."))
        self.assertNotIn("PRIVATE KEY", secret.sealed)
        material = CertificateMaterial.objects.get()
        held = str([getattr(material, field.attname) for field in material._meta.concrete_fields])
        self.assertNotIn("PRIVATE KEY", held)
        self.resource.refresh_from_db()
        self.assertNotIn("PRIVATE KEY", str(self.resource.spec))

    def test_the_controller_gets_the_key_back_only_by_opening_it(self):
        self._store()

        material = material_for(self.resource.key)

        self.assertNotIn("PRIVATE KEY", str(material))
        self.assertEqual(opened(material["slots"][KEY_SLOT], self.resource.key), self.private_key)
        self.assertEqual(material["fullchain"], self.fullchain)
        self.assertEqual(material["domains"], ["newhost.example.test"])

    def test_the_audit_record_names_the_slot_and_version_and_nothing_of_the_value(self):
        self._store()
        self._store()

        events = AuditLog.objects.filter(metadata__slot=KEY_SLOT).order_by("pk")
        self.assertEqual([event.metadata["version"] for event in events], [1, 2])
        self.assertEqual([event.metadata["change"] for event in events], ["set", "replaced"])
        sealed = self.resource.sealed_secrets.get().sealed
        for event in events:
            recorded = f"{event.message} {event.metadata} {event.object_repr}"
            self.assertNotIn("PRIVATE KEY", recorded)
            self.assertNotIn(sealed, recorded)

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
        self.assertNotIn("HQ2.", serialized)

    def test_the_response_does_not_hand_the_secret_back(self):
        """A response body is the easiest place for a secret to end up in a log."""
        result = self._store()

        self.assertNotIn("private_key", result)
        self.assertNotIn("fullchain", result)
        self.assertNotIn("slots", result)

    def test_uploading_again_replaces_rather_than_accumulates(self):
        self._store()
        self.fullchain, self.private_key = a_certificate()
        self._store()

        self.assertEqual(CertificateMaterial.objects.count(), 1)
        (secret,) = self.resource.sealed_secrets.all()
        self.assertEqual(secret.version, 2)
        self.assertEqual(
            opened(material_for(self.resource.key)["slots"][KEY_SLOT], self.resource.key), self.private_key
        )

    def test_a_rejected_pair_stores_nothing(self):
        _, other_key = a_certificate("other.example.test")

        with self.assertRaises(CertificateError):
            store_certificate(
                UploadCertificateCommand(
                    fullchain=self.fullchain,
                    private_key=other_key,
                ),
                principal=cli_principal(),
                current_key=self.resource.key,
            )

        self.assertFalse(CertificateMaterial.objects.exists())
        self.assertFalse(self.resource.sealed_secrets.exists())


class FailClosedTests(TestCase):
    """No key to seal to means no storage, not quiet plaintext."""

    def test_no_seal_key_refuses_the_upload(self):
        resource = ManagedResource.objects.create(
            key="newhost-cert",
            kind="tls.uploaded_certificate",
            spec={"certificate_name": "n", "install_on": ["example-npm"]},
        )
        fullchain, private_key = a_certificate()

        for value in ("", "not a key"):
            with (
                self.subTest(key=value),
                override_settings(SEVERINO_SEAL_PUBLIC_KEY=value),
                self.assertRaisesMessage(PolicyError, secrets.NO_SEAL_KEY),
            ):
                store_certificate(
                    UploadCertificateCommand(fullchain=fullchain, private_key=private_key),
                    principal=cli_principal(),
                    current_key=resource.key,
                )

        self.assertFalse(CertificateMaterial.objects.exists())
        self.assertFalse(resource.sealed_secrets.exists())
        resource.refresh_from_db()
        self.assertEqual(resource.generation, 1)


@override_settings(SEVERINO_SEAL_PUBLIC_KEY=SEAL_PUBLIC_KEY)
class CoverageTests(TestCase):
    """A certificate HQ was given covers the names it carries.

    Otherwise every private name it answers for reads as "no declared
    certificate covers it": permanently, on the service page and in the
    attention queue. That is the whole reason this kind exists: for a
    name no public authority will issue for, an internally signed certificate is
    the only possible answer.
    """

    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.uploaded_certificate",
            spec={
                "certificate_name": "example-wildcard",
                "install_on": ["a-proxy"],
            },
        )

    def upload(self, name="grafana.example"):
        fullchain, private_key = a_certificate(name)
        store_certificate(
            UploadCertificateCommand(fullchain=fullchain, private_key=private_key),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        self.resource.refresh_from_db()

    def covered(self):
        from hq.domains.control_plane.providers import PROVIDERS

        from ..infrastructure import resolved_spec

        provider = PROVIDERS[self.resource.kind]
        return tuple(provider.hostnames(resolved_spec(self.resource)))

    def test_it_covers_what_the_uploaded_certificate_carries(self):
        self.upload("grafana.example")

        self.assertEqual(self.covered(), ("grafana.example",))

    def test_it_covers_them_before_the_controller_has_ever_run(self):
        """HQ read the certificate, so it does not need to be told twice.

        Waiting for the first pass would leave the name uncovered at the one
        moment an operator is certain to look: right after uploading a certificate for it.
        """

        self.upload("grafana.example")

        self.assertEqual(self.resource.observed_generation, 0)
        self.assertEqual(self.covered(), ("grafana.example",))

    def test_one_HQ_has_not_been_given_yet_covers_nothing(self):
        self.assertEqual(self.covered(), ())

    def test_the_service_it_serves_stops_asking_for_a_certificate(self):
        from hq.domains.control_plane.models import ManagedResource as Resource

        from ..services import prospects

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

        service = prospects(("grafana.example",))[0]

        self.assertNotIn("no declared certificate covers it", " ".join(service.faults))

    def test_uploading_asks_for_it_to_be_installed(self):
        """The page promises a pass; something has to give that pass a reason."""

        self.resource.observed_generation = self.resource.generation
        self.resource.save(update_fields=["observed_generation"])

        self.upload("grafana.example")

        self.assertGreater(self.resource.generation, self.resource.observed_generation)

    def test_replacing_it_with_a_renewal_asks_again(self):
        """A renewal covers the same names, and still has to be installed."""

        self.upload("grafana.example")
        self.resource.observed_generation = self.resource.generation
        self.resource.save(update_fields=["observed_generation"])

        self.upload("grafana.example")

        self.assertGreater(self.resource.generation, self.resource.observed_generation)

    def test_a_proxy_can_be_pointed_at_one_from_the_form(self):
        """The only possible answer for a private name is on the menu."""

        from hq.domains.control_plane.provider_spec import NameContext

        from ..provider_choices import proxy_choices

        self.upload("grafana.example")
        offered = dict(proxy_choices(NameContext(hostname="grafana.example"))["certificate_resource"])

        self.assertIn("example-wildcard", offered)


class UploadFormTests(SimpleTestCase):
    def test_a_form_shown_again_never_carries_the_submitted_key(self):
        from ..provider_forms import CertificateUploadForm

        form = CertificateUploadForm(
            data={
                "fullchain": "-----BEGIN CERTIFICATE-----\nexample\n-----END CERTIFICATE-----",
                "private_key": "-----BEGIN PRIVATE KEY-----\nexample-secret-material\n-----END PRIVATE KEY-----",
            }
        )

        rendered = str(form["private_key"])

        self.assertNotIn("example-secret-material", rendered)
        self.assertEqual([field.tag for field in all_elements(rendered)], ["textarea"])


@override_settings(SEVERINO_SEAL_PUBLIC_KEY=SEAL_PUBLIC_KEY)
class UploadCapabilityTests(TestCase):
    """Who may hand HQ a private key: an operator in the browser or at the host, and nobody else."""

    def setUp(self):
        self.resource = ManagedResource.objects.create(
            key="newhost-cert",
            kind="tls.uploaded_certificate",
            spec={"certificate_name": "newhost.example.test", "install_on": ["example-npm"]},
        )
        self.fullchain, self.private_key = a_certificate()

    def upload(self, principal, *, target="newhost-cert", private_key=None):
        return execute_capability(
            UPLOAD,
            keyed({"fullchain": self.fullchain, "private_key": private_key or self.private_key}),
            principal=principal,
            target=target,
        )

    def test_an_operator_uploads_from_the_browser_or_the_host(self):
        for principal in (Principal("operator", "web", OPERATOR_CAPABILITIES), cli_principal()):
            with self.subTest(interface=principal.interface):
                result = self.upload(principal)

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["domains"], ["newhost.example.test"])
                self.assertNotIn(self.private_key, str(result))
        self.assertEqual(CertificateMaterial.objects.count(), 1)

    def test_a_token_is_refused_whatever_it_was_granted_before_anything_is_held(self):
        granted = frozenset({Capability.UPLOAD_CERTIFICATE, Capability.MANAGE_INFRASTRUCTURE})
        for principal in (
            Principal("example-client", "api", granted, granted=granted),
            Principal("example-agent", "mcp", granted, granted=granted),
            mcp_principal(),
            internal_principal("example-purpose"),
        ):
            with self.subTest(interface=principal.interface):
                result = self.upload(principal)

                self.assertEqual(result["error"]["code"], "forbidden")
                self.assertNotIn(self.private_key, str(result))
        self.assertFalse(CertificateMaterial.objects.exists())
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=UPLOAD).count(), 4)

    def test_unusable_material_is_refused_with_its_reason(self):
        _, other_key = a_certificate("other.example.test")
        try:
            inspect(self.fullchain, other_key)
        except CertificateError as exc:
            reason = exc.reason

        result = self.upload(cli_principal(), private_key=other_key)

        self.assertEqual(result["error"], {"code": "invalid_material", "message": reason})
        self.assertFalse(CertificateMaterial.objects.exists())

    def test_only_an_uploaded_certificate_record_holds_material(self):
        ManagedResource.objects.create(
            key="app-dns", kind="adguard.rewrite", spec={"domain": "app.example.com", "answer": "192.0.2.10"}
        )

        missing = self.upload(cli_principal(), target="no-such-record")
        elsewhere = self.upload(cli_principal(), target="app-dns")

        self.assertEqual(missing["error"]["code"], "not_found")
        self.assertEqual(elsewhere["error"]["code"], "precondition")
        self.assertFalse(CertificateMaterial.objects.exists())

    @override_settings(SEVERINO_SEAL_PUBLIC_KEY="")
    def test_nowhere_safe_to_put_it_is_said(self):
        result = self.upload(cli_principal())

        self.assertEqual(result["error"], {"code": "precondition", "message": secrets.NO_SEAL_KEY})
        self.assertFalse(CertificateMaterial.objects.exists())
