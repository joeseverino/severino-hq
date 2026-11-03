"""Where a certificate goes: consumers derived from targets, confirmed by verifying, recorded in places."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from pydantic import ValidationError

from hq.platform.application.controller import ControllerReport, report_operation
from hq.platform.application.infrastructure import resolved_spec

from ..models import ManagedResource, OperationRequest, ProviderConnection, ProviderInventory
from ..provider_adapters.declarations import TLSDeliveryTargetSpec
from ..provider_adapters.tls import OnePasswordPublication
from ..provider_spec import ProviderResolutionContext
from ..providers import resolve_provider_spec
from .certificate_cases import certificate_spec, declare_targets


class DerivedConsumerTests(TestCase):
    """Who consumes a certificate is derived, not typed into a list.

    A hand-written set of names on a target misses a second name served from
    the same box, while that name's own service page, which asks what the
    certificate covers, still shows it: two answers to one question.
    """

    def _estate(self):
        declare_targets()
        ProviderConnection.objects.create(
            connection_ref="edge",
            provider="ssh",
            endpoint="10.0.0.19:7722",
            observed_at=timezone.now(),
        )
        ProviderInventory.objects.create(
            kind="adguard.rewrite",
            observed_at=timezone.now(),
            records=[
                # Covered by the wildcard and answering at the edge box, so a
                # consumer whether or not anybody wrote it down.
                {"domain": "status.example.com", "answer": "10.0.0.19"},
                # The same box, and not a name this certificate covers.
                {"domain": "unrelated.invalid", "answer": "10.0.0.19"},
                # Covered, and somewhere else entirely.
                {"domain": "elsewhere.example.com", "answer": "10.0.0.44"},
            ],
        )
        return ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

    def test_a_consumer_is_a_covered_name_served_at_the_target_or_a_written_one(self):
        """A covered name answering at the target is a consumer without being
        listed, and a written name is kept beside it, since a target may serve
        a name no sweep can see. Sharing a host without being covered, or being
        covered and answering elsewhere, is not consuming."""

        resolved = resolved_spec(self._estate())
        consumer = next(consumer for consumer in resolved["consumers"] if consumer["connection_ref"] == "edge")

        self.assertEqual(set(consumer["verify_domains"]), {"status.example.com", "health.example.com"})

    def test_a_target_reached_by_name_is_placed_like_one_reached_by_address(self):
        """The endpoint is a name, so it is one hop from being a machine.

        The index files a URL endpoint under the connection's own ref, which is
        what stops a proxy forwarding at a credentialled host reading as
        "unknown host". Taken as the placement it puts the connection at itself,
        and nothing landing on the actual box is ever matched to it.
        """

        declare_targets()
        ProviderConnection.objects.create(
            connection_ref="edge",
            provider="ssh",
            endpoint="https://edge.example.com",
            observed_at=timezone.now(),
        )
        ProviderInventory.objects.create(
            kind="adguard.rewrite",
            observed_at=timezone.now(),
            records=[
                {"domain": "edge.example.com", "answer": "10.0.0.19"},
                {"domain": "status.example.com", "answer": "10.0.0.19"},
            ],
        )
        certificate = ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

        consumer = next(item for item in resolved_spec(certificate)["consumers"] if item["connection_ref"] == "edge")

        self.assertIn("status.example.com", consumer["verify_domains"])

    def test_what_is_only_declared_is_not_yet_evidence(self):
        """A record HQ holds and has not applied is an intention.

        Counting it would let a certificate report a consumer no request has
        reached, which is the same false confidence in the other direction.
        """

        declare_targets()
        ProviderConnection.objects.create(
            connection_ref="edge",
            provider="ssh",
            endpoint="10.0.0.19:7722",
            observed_at=timezone.now(),
        )
        ManagedResource.objects.create(
            key="pending-dns",
            kind="adguard.rewrite",
            spec={"domain": "pending.example.com", "answer": "10.0.0.19"},
        )
        certificate = ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

        consumer = next(item for item in resolved_spec(certificate)["consumers"] if item["connection_ref"] == "edge")

        self.assertNotIn("pending.example.com", consumer["verify_domains"])


class DeliveryTargetConfirmationTests(TestCase):
    """Verifying a certificate is the only thing that ever sees its targets.

    Nothing sweeps a delivery target, but every reconcile opens a connection to
    it, reads back what it serves and matches the fingerprint. That evidence
    confirms the target, or its "last confirmed" says *never* forever.
    """

    def _report(self, consumers):
        certificate = ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )
        operation = OperationRequest.objects.create(
            resource=certificate,
            action=OperationRequest.Action.RECONCILE,
            idempotency_key="verify-once",
            state=OperationRequest.State.CLAIMED,
            claimed_by="example-controller",
            lease_expires_at=timezone.now() + timedelta(minutes=5),
            input={"generation": certificate.generation},
        )
        report_operation(
            str(operation.pk),
            ControllerReport(
                success=True,
                observed_generation=certificate.generation,
                status={"consumers": consumers},
            ),
            controller_id="example-controller",
        )

    def _confirmed(self, connection_ref):
        return ManagedResource.objects.get(key=f"{connection_ref}-certificate-target").last_observed_at

    def test_only_the_target_the_certificate_was_verified_at_is_confirmed(self):
        """Confirming all three off one observation would be inventing evidence."""

        declare_targets()

        self._report([{"consumer": "edge-caddy", "domain": "health.example.com"}])

        self.assertIsNotNone(self._confirmed("edge"))
        self.assertIsNone(self._confirmed("a-shared-host"))

    def test_a_report_with_no_observations_confirms_nothing(self):
        declare_targets()

        self._report([])

        self.assertIsNone(self._confirmed("edge"))

    # No generic "every row can hold a value" test here. Rows that read lists
    # (a certificate's consumers, the tailnet policy's grants, groups and tests)
    # stay blank under a synthetic status, because a fixture cannot fake a list
    # without knowing its shape. That check needs real readings, so it is an
    # audit rather than a test.


class PublishingFactsIsDeclaredAsAPlaceNotAsContentTests(TestCase):
    """A certificate can name somewhere that records it rather than serves it.

    The security property this pins is where the two halves of the decision
    live. A declaration is operator input (it sits in HQ's database and is
    edited through a form) so it says only which connection, which vault and
    which item. What gets written is fixed in the adapter. So the resolved shape
    has to carry addressing and nothing else: no field for a value, no field for
    a label, no field for a sixth fact.
    """

    A_PROXY_TARGET = {
        "kind": "npm",
        "connection_ref": "a-proxy",
        "name": "a-proxy-certificate",
        "certificate_resource": "",
        "verify_domains": ["shop.example.test"],
        "certificate_directory": "",
        "discover_covered_hosts": False,
        "install_domains": [],
        "vault": "",
        "item": "",
    }
    A_VAULT_TARGET = {
        "kind": "onepassword",
        "connection_ref": "a-password-manager",
        "name": "where-certificates-are-noted",
        "certificate_resource": "",
        "verify_domains": [],
        "certificate_directory": "",
        "discover_covered_hosts": False,
        "install_domains": [],
        "vault": "An Example Vault",
        "item": "An Example Certificate Item",
    }

    def _resolve(self, *targets, install_on=None):
        return resolve_provider_spec(
            "tls.certificate",
            {
                "certificate_name": "an-example-certificate",
                "domains": ["shop.example.test", "*.shop.example.test"],
                "install_on": install_on or [target["connection_ref"] for target in targets],
                "renewal_window_days": 30,
            },
            context=ProviderResolutionContext(delivery_targets=targets),
        )

    def test_a_shared_hosting_target_without_a_list_is_left_to_the_account(self):
        """Empty means every site that serves a checked name, decided at run time.

        Never every name the certificate covers: that would name domains the
        account does not host.
        """

        target = {
            **self.A_PROXY_TARGET,
            "kind": "cpanel",
            "connection_ref": "shared-hosting",
            "name": "shared-hosting",
            "verify_domains": ["www.shop.example.test"],
        }

        resolved = self._resolve(target)

        (consumer,) = resolved["consumers"]
        self.assertEqual(consumer["install_domains"], [])
        self.assertEqual(consumer["verify_domains"], ["www.shop.example.test"])

    def test_a_recording_target_is_not_resolved_as_a_consumer(self):
        """A consumer is something HQ connects to and asks what it is serving.

        Folded into that list, a password manager would be probed for a TLS
        handshake it cannot answer, and the certificate would report a consumer
        failing to serve it.
        """

        resolved = self._resolve(self.A_PROXY_TARGET, self.A_VAULT_TARGET)

        self.assertEqual([consumer["kind"] for consumer in resolved["consumers"]], ["npm"])
        self.assertEqual(
            [publication["kind"] for publication in resolved["publish_to"]],
            ["onepassword"],
        )

    def test_a_publication_carries_addressing_and_nothing_else(self):
        """The declaration's whole vocabulary, so the code owns the content."""

        publication = self._resolve(self.A_PROXY_TARGET, self.A_VAULT_TARGET)["publish_to"][0]

        self.assertEqual(
            set(publication),
            {"kind", "name", "connection_ref", "vault", "item"},
        )

    def test_a_declaration_cannot_add_a_field_to_a_publication(self):
        """`extra="forbid"` is the mechanism; this is the intent it protects.

        Anything a form could grow that named a field, a label or a value would
        be refused at the boundary rather than carried to the adapter and
        quietly written to a password manager.
        """

        with self.assertRaises(ValidationError):
            OnePasswordPublication(
                kind="onepassword",
                name="a-publication",
                connection_ref="a-password-manager",
                vault="An Example Vault",
                item="An Example Certificate Item",
                fields={"Recovery Phrase": "take this too"},
            )

    def test_a_certificate_that_is_only_recorded_is_refused(self):
        """Noting a certificate down is not installing it anywhere."""

        with self.assertRaisesRegex(ValueError, "has no install target"):
            self._resolve(self.A_VAULT_TARGET)

    def test_a_recording_target_declares_no_name_to_check_it_at(self):
        """Nothing is served there, so a name typed here would never be probed."""

        with self.assertRaisesRegex(ValueError, "serves nothing"):
            TLSDeliveryTargetSpec(
                kind="onepassword",
                connection_ref="a-password-manager",
                name="where-certificates-are-noted",
                vault="An Example Vault",
                item="An Example Certificate Item",
                verify_domains=["shop.example.test"],
            )

    def test_a_recording_target_needs_both_the_vault_and_the_item(self):
        with self.assertRaisesRegex(ValueError, "needs a vault and an item"):
            TLSDeliveryTargetSpec(
                kind="onepassword",
                connection_ref="a-password-manager",
                name="where-certificates-are-noted",
                vault="An Example Vault",
            )

    def test_a_vault_named_against_another_kind_of_target_is_refused(self):
        """Ignored, it would sit there looking configured and be read by nothing."""

        with self.assertRaisesRegex(ValueError, "applies only to onepassword targets"):
            TLSDeliveryTargetSpec(
                kind="npm",
                connection_ref="a-proxy",
                name="a-proxy-certificate",
                vault="An Example Vault",
            )

    def test_a_certificate_hq_did_not_issue_cannot_be_recorded(self):
        """There are no observed facts to publish, only the declaration."""

        with self.assertRaisesRegex(ValueError, "cannot be recorded in 1Password"):
            resolve_provider_spec(
                "tls.uploaded_certificate",
                {
                    "certificate_name": "an-example-private-certificate",
                    "install_on": ["a-password-manager"],
                },
                context=ProviderResolutionContext(delivery_targets=(self.A_VAULT_TARGET,)),
            )
