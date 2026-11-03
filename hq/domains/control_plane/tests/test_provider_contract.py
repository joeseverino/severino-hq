"""What the controller is handed: the whole spec, from one owner, by every provider alike."""

from django.test import TestCase

from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.infrastructure import (
    ManagedResourceCommand,
    controller_contract,
    delivery_targets,
    resolved_spec,
    save_managed_resource,
)
from hq.platform.application.record_errors import PolicyError
from hq.platform.application.security import cli_principal

from ..desired_state import advance_dependents
from ..models import ManagedResource
from ..providers import PROVIDERS, describe_providers
from .certificate_cases import DOMAINS, certificate_spec, declare_targets


def validate_resolved_certificate(payload):
    """The resolved-state validation ``resolve_provider_spec`` applies to a certificate."""

    from pydantic import TypeAdapter

    resolved = TypeAdapter(PROVIDERS["tls.certificate"].resolved_type)
    return resolved.dump_python(resolved.validate_python(payload), mode="json")


def resolved_certificate_spec():
    return {
        "certificate_name": "example",
        "domains": list(DOMAINS),
        "consumers": [
            {
                "kind": "npm",
                "name": "example-wildcard",
                "connection_ref": "example-npm",
                "verify_domains": ["dev.example.com"],
                "discover_covered_hosts": False,
            },
            {
                "kind": "caddy",
                "name": "edge-caddy",
                "connection_ref": "edge",
                "certificate_directory": "/opt/apps/caddy/certs",
                "verify_domains": ["health.example.com"],
            },
            {
                "kind": "cpanel",
                "name": "namecheap-shared-hosting",
                "connection_ref": "a-shared-host",
                "install_domains": ["example.com", "example.net"],
                "verify_domains": ["example.com", "quiz.example.net"],
            },
        ],
        "renewal_window_days": 30,
    }


class ControllerContractCompletenessTests(TestCase):
    """A spec stored before a field existed still reaches the controller whole.

    The controller indexes spec fields directly, so a missing key is a crash
    mid-reconciliation rather than a default. Stored specs may predate a field,
    so the contract validates through pydantic on the way out to fill it.
    """

    def test_a_spec_missing_a_newly_added_field_is_filled_by_the_contract(self):
        resource = ManagedResource.objects.create(
            key="legacy-proxy",
            kind="npm.proxy_host",
            # A spec written before hsts and serving existed.
            spec={
                "domain_names": ["app.example.com"],
                "forward_scheme": "http",
                "forward_host": "10.0.0.10",
                "forward_port": 8000,
                "certificate_resource": "",
                "force_ssl": True,
                "http2": True,
                "websocket": False,
                "caching_enabled": False,
                "block_exploits": True,
                "access_list_id": 0,
                "advanced_config": "",
            },
        )

        spec = controller_contract(resource)["resource"]["spec"]

        for field in PROVIDERS["npm.proxy_host"].spec_type.model_fields:
            self.assertIn(field, spec, f"{field} would KeyError in the controller")


class DesiredStateOwnershipTests(TestCase):
    """HQ holds every part of the answer, including the parts it resolves."""

    def setUp(self):
        super().setUp()
        managing_everything()

    def _certificate(self):
        declare_targets()
        return ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

    def test_a_certificate_says_what_it_covers_without_being_resolved(self):
        """The names are on the resource, so nothing else has to be present."""

        resource = self._certificate()

        self.assertEqual(resource.spec["domains"], DOMAINS)

    def test_a_target_supplies_the_settings_the_certificate_does_not(self):
        resolved = resolved_spec(self._certificate())

        self.assertEqual(resolved["consumers"], resolved_certificate_spec()["consumers"])

    def test_a_certificate_naming_a_target_that_is_gone_covers_nothing(self):
        """Reported as an uncovered name, which is exactly what is true."""

        resource = self._certificate()
        ManagedResource.objects.filter(kind="tls.delivery_target").delete()

        self.assertNotIn("consumers", resolved_spec(resource))

    def test_a_second_certificate_is_named_after_itself_at_a_shared_target(self):
        """Two certificates at one target must not collide on one name."""

        self._certificate()
        other = ManagedResource.objects.create(
            key="another",
            kind="tls.certificate",
            spec={
                **certificate_spec(),
                "certificate_name": "another",
                "install_on": ["edge"],
            },
        )

        self.assertEqual(resolved_spec(other)["consumers"][0]["name"], "another-caddy")

    def test_editing_a_target_advances_the_certificates_installed_there(self):
        """The authored spec is byte-identical; what it resolves to is not.

        A certificate names where it installs. Change how that place takes a
        certificate and the controller has different work to do, while the spec
        HQ holds has not moved at all.
        """

        resource = self._certificate()
        advance_dependents(delivery_targets())
        resource.refresh_from_db()
        settled = resource.generation

        target = ManagedResource.objects.get(key="example-npm-certificate-target")
        target.spec = {**target.spec, "discover_covered_hosts": True}
        target.save(update_fields=["spec"])
        advance_dependents(delivery_targets())

        resource.refresh_from_db()
        self.assertEqual(resource.generation, settled + 1)

    def test_a_first_fingerprint_adopts_without_queueing_work(self):
        """Otherwise every resource looks changed at once the first time.

        Each would queue an operation against a provider for a difference that
        does not exist.
        """

        resource = self._certificate()
        self.assertEqual(resource.desired_fingerprint, "")

        advance_dependents(delivery_targets())

        resource.refresh_from_db()
        self.assertTrue(resource.desired_fingerprint)
        self.assertEqual(resource.generation, 1)

    def test_saving_a_target_advances_them_without_a_separate_step(self):
        """Editing the target is the whole action; nothing else has to be run."""

        resource = self._certificate()
        advance_dependents(delivery_targets())
        resource.refresh_from_db()
        settled = resource.generation

        target = ManagedResource.objects.get(key="edge-certificate-target")
        save_managed_resource(
            ManagedResourceCommand(
                key=target.key,
                kind=target.kind,
                spec={**target.spec, "certificate_directory": "/srv/certs"},
            ),
            principal=cli_principal(),
            current_key=target.key,
        )

        resource.refresh_from_db()
        self.assertEqual(resource.generation, settled + 1)

    def test_nothing_moves_when_nothing_resolved_differently(self):
        resource = self._certificate()
        advance_dependents(delivery_targets())
        resource.refresh_from_db()
        settled, stamped = resource.generation, resource.updated_at

        advance_dependents(delivery_targets())

        resource.refresh_from_db()
        self.assertEqual(resource.generation, settled)
        self.assertEqual(resource.updated_at, stamped)


class RegistrySymmetryTests(TestCase):
    """Providers that answer the same question must answer all of it.

    Two providers supplying one facet, one declaring a hook the other does not,
    make a page report HQ's own silence as a fact about the world.
    """

    def test_a_provider_that_says_where_a_name_points_says_where_it_is_served(self):
        """Answering for a name and routing it are the same knowledge.

        A provider that can say which address a name resolves to can say where
        that name is served: the address *is* the answer. Declaring the first
        and withholding the second leaves a name carried by that provider with
        no origin, no machine and no runtime, reported as "nothing supplies
        this" while the box serving it is in HQ's own inventory.
        """

        silent = sorted(
            kind for kind, provider in PROVIDERS.items() if provider.answers is not None and provider.origin is None
        )

        self.assertEqual(
            silent,
            [],
            "these say where a name resolves but not where it is served, so a "
            "name they carry alone will read as unrouted: " + ", ".join(silent),
        )

    def test_every_facet_provider_agrees_on_what_a_hostname_is(self):
        """A facet is a column, and two providers must fill it the same way."""

        missing = sorted(
            kind
            for kind, provider in PROVIDERS.items()
            if provider.facet and provider.covers and provider.hostnames is None
        )

        self.assertEqual(missing, [], f"covering providers with no names: {missing}")


class ProviderContractTests(TestCase):
    def test_resolved_certificate_accepts_wildcard_covered_cpanel_vhost(self):
        spec = resolved_certificate_spec()
        cpanel = next(item for item in spec["consumers"] if item["kind"] == "cpanel")
        cpanel["install_domains"] = ["quiz.example.net"]

        validate_resolved_certificate(spec)

    def test_provider_catalog_is_stable_strict_and_marks_public_effects(self):
        self.assertEqual(describe_providers(), describe_providers())
        providers = {item["kind"]: item for item in describe_providers()["providers"]}
        self.assertFalse(providers["adguard.rewrite"]["public_effect"])
        self.assertTrue(providers["cloudflare.dns_record"]["public_effect"])
        self.assertEqual(
            providers["adguard.rewrite"]["controller"]["actions"]["reconcile"]["mode"],
            "apply",
        )
        self.assertEqual(
            providers["tls.certificate"]["controller"]["actions"]["renew"]["mode"],
            "apply",
        )
        self.assertFalse(providers["tls.certificate"]["spec_schema"]["additionalProperties"])
        self.assertIn(
            "install_on",
            providers["tls.certificate"]["spec_schema"]["properties"],
        )

    def test_certificate_contract_normalizes_and_validates_deployments(self):
        spec = resolved_certificate_spec()
        spec["domains"][0] = "EXAMPLE.COM."
        self.assertEqual(
            validate_resolved_certificate(spec)["domains"][0],
            "example.com",
        )

        spec["consumers"][-1]["install_domains"].append("not-covered.example")
        with self.assertRaisesRegex(ValueError, "not-covered.example"):
            validate_resolved_certificate(spec)

    def test_invalid_provider_payload_fails_before_persistence(self):
        result = execute_capability(
            "infrastructure.resource.create",
            {
                "key": "bad-proxy",
                "kind": "npm.proxy_host",
                "spec": {
                    "domain_names": ["dev-hq.example.com"],
                    "forward_scheme": "http",
                    "forward_host": "100.64.0.7",
                    "forward_port": 70000,
                    "unexpected": "never accepted",
                },
            },
            principal=cli_principal(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ManagedResource.objects.exists())

    def test_public_dns_is_declarable_disabled_but_cannot_be_enabled(self):
        disabled = save_managed_resource(
            ManagedResourceCommand(
                key="future-public-hq",
                kind="cloudflare.dns_record",
                enabled=False,
                spec={
                    "zone": "example.com",
                    "name": "hq.example.com",
                    "record_type": "A",
                    "content": "192.0.2.1",
                },
            ),
            principal=cli_principal(),
        )
        self.assertTrue(disabled["ok"])
        with self.assertRaisesRegex(PolicyError, "Public DNS changes are off"):
            save_managed_resource(
                ManagedResourceCommand(
                    key="public-hq",
                    kind="cloudflare.dns_record",
                    spec={
                        "zone": "example.com",
                        "name": "hq.example.com",
                        "record_type": "A",
                        "content": "192.0.2.1",
                    },
                ),
                principal=cli_principal(),
            )
