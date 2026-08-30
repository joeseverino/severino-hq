from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from application.capabilities import execute_capability
from application.controller import (
    ControllerReport,
    report_operation,
    schedule_automatic_operations,
)
from application.infrastructure import (
    ManagedResourceCommand,
    OperationCommand,
    PolicyError,
    controller_contract,
    request_certificate_renewal,
    request_reconcile,
    save_managed_resource,
)
from application.security import cli_principal, mcp_principal

from .models import (
    ManagedResource,
    OperationRequest,
    ProviderConnection,
    ProviderInventory,
)
from .providers import (
    NPMProxyHostSpec,
    describe_providers,
    validate_resolved_certificate,
)
from application.infrastructure import delivery_targets as delivery_targets_for_test

from .desired_state import advance_dependents


DOMAINS = [
    "jseverino.com",
    "*.jseverino.com",
    "jseverino.net",
    "*.jseverino.net",
    "jseverino.org",
    "*.jseverino.org",
    "joeseverino.com",
    "*.joeseverino.com",
]


def certificate_spec():
    return {
        "certificate_name": "jseverino",
        "domains": list(DOMAINS),
        "install_on": ["homelab-npm", "edge", "a-shared-host"],
        "renewal_window_days": 30,
    }


TARGETS = (
    {
        "kind": "npm",
        "connection_ref": "homelab-npm",
        "name": "jseverino-wildcard",
        "certificate_resource": "jseverino-wildcard",
        "verify_domains": ["dev.jseverino.com"],
        "discover_covered_hosts": False,
    },
    {
        "kind": "caddy",
        "connection_ref": "edge",
        "name": "edge-caddy",
        "certificate_resource": "jseverino-wildcard",
        "verify_domains": ["health.example.com"],
        "certificate_directory": "/opt/apps/caddy/certs",
    },
    {
        "kind": "cpanel",
        "connection_ref": "a-shared-host",
        "name": "namecheap-shared-hosting",
        "certificate_resource": "jseverino-wildcard",
        "verify_domains": ["jseverino.com", "quiz.jseverino.net"],
        "install_domains": ["jseverino.com", "jseverino.net"],
    },
)


def declare_targets():
    """The three places the certificate in these tests is installed."""

    return [
        ManagedResource.objects.create(
            key=f"{spec['connection_ref']}-certificate-target",
            kind="tls.delivery_target",
            spec=dict(spec),
        )
        for spec in TARGETS
    ]


def resolved_certificate_spec():
    return {
        "certificate_name": "jseverino",
        "domains": list(DOMAINS),
        "consumers": [
            {
                "kind": "npm",
                "name": "jseverino-wildcard",
                "connection_ref": "homelab-npm",
                "verify_domains": ["dev.jseverino.com"],
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
                "install_domains": ["jseverino.com", "jseverino.net"],
                "verify_domains": ["jseverino.com", "quiz.jseverino.net"],
            },
        ],
        "renewal_window_days": 30,
    }


class ControllerContractCompletenessTests(TestCase):
    """A spec stored before a field existed still reaches the controller whole.

    The controller indexes spec fields directly, so a missing key is a crash
    mid-reconciliation rather than a default. Specs are stored as JSON and were
    written by older versions of the model, so the only thing standing between
    an added field and a broken production reconcile is that the contract
    validates through pydantic on the way out.
    """

    def test_a_spec_missing_a_newly_added_field_is_filled_by_the_contract(self):
        resource = ManagedResource.objects.create(
            key="legacy-proxy",
            kind="npm.proxy_host",
            # Exactly what production held before hsts and serving existed.
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

        for field in NPMProxyHostSpec.model_fields:
            self.assertIn(field, spec, f"{field} would KeyError in the controller")


class DerivedConsumerTests(TestCase):
    """Who consumes a certificate is derived, not typed into a list.

    The bug this pins: a target carried a hand-written set of names to verify,
    so a second name served from the same box was a consumer in reality and
    absent from the certificate. Its own service page showed the certificate
    correctly the whole time -- that side asks what the certificate covers --
    which is what made two answers to one question so hard to see.
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
                {"domain": "status.jseverino.com", "answer": "10.0.0.19"},
                # The same box, and not a name this certificate covers.
                {"domain": "unrelated.example.com", "answer": "10.0.0.19"},
                # Covered, and somewhere else entirely.
                {"domain": "elsewhere.jseverino.com", "answer": "10.0.0.44"},
            ],
        )
        return ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

    def _caddy_consumer(self):
        from application.infrastructure import resolved_spec

        resolved = resolved_spec(self._estate())
        return next(
            consumer
            for consumer in resolved["consumers"]
            if consumer["connection_ref"] == "edge"
        )

    def test_a_name_served_at_the_target_is_a_consumer_without_being_listed(self):
        self.assertIn("status.jseverino.com", self._caddy_consumer()["verify_domains"])

    def test_the_written_names_are_kept_beside_the_derived_ones(self):
        """A target may serve a name no sweep can see, so declaring still adds."""

        self.assertIn("health.example.com", self._caddy_consumer()["verify_domains"])

    def test_a_name_the_certificate_does_not_cover_is_not_a_consumer(self):
        """Sharing a host is not being covered by the same certificate."""

        self.assertNotIn(
            "unrelated.example.com", self._caddy_consumer()["verify_domains"]
        )

    def test_a_covered_name_answering_elsewhere_is_not_a_consumer(self):
        """Coverage alone would make every name a consumer of every target."""

        self.assertNotIn(
            "elsewhere.jseverino.com", self._caddy_consumer()["verify_domains"]
        )

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
                {"domain": "status.jseverino.com", "answer": "10.0.0.19"},
            ],
        )
        certificate = ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

        from application.infrastructure import resolved_spec

        consumer = next(
            item
            for item in resolved_spec(certificate)["consumers"]
            if item["connection_ref"] == "edge"
        )

        self.assertIn("status.jseverino.com", consumer["verify_domains"])

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
            spec={"domain": "pending.jseverino.com", "answer": "10.0.0.19"},
        )
        certificate = ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

        from application.infrastructure import resolved_spec

        consumer = next(
            item
            for item in resolved_spec(certificate)["consumers"]
            if item["connection_ref"] == "edge"
        )

        self.assertNotIn("pending.jseverino.com", consumer["verify_domains"])


class DesiredStateOwnershipTests(TestCase):
    """HQ holds every part of the answer, including the parts it resolves."""

    def _certificate(self):
        declare_targets()
        return ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )

    def test_a_certificate_says_what_it_covers_without_being_resolved(self):
        """The names are on the resource, so nothing else has to be present."""

        resource = self._certificate()

        self.assertEqual(resource.spec["domains"], DOMAINS)

    def test_a_target_supplies_the_settings_the_certificate_does_not(self):
        from application.infrastructure import resolved_spec

        resolved = resolved_spec(self._certificate())

        self.assertEqual(
            resolved["consumers"], resolved_certificate_spec()["consumers"]
        )

    def test_a_certificate_naming_a_target_that_is_gone_covers_nothing(self):
        """Reported as an uncovered name, which is exactly what is true."""

        from application.infrastructure import resolved_spec

        resource = self._certificate()
        ManagedResource.objects.filter(kind="tls.delivery_target").delete()

        self.assertNotIn("consumers", resolved_spec(resource))

    def test_a_second_certificate_is_named_after_itself_at_a_shared_target(self):
        """Two certificates at one target must not collide on one name."""

        from application.infrastructure import resolved_spec

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

        from application.infrastructure import delivery_targets

        resource = self._certificate()
        advance_dependents(delivery_targets())
        resource.refresh_from_db()
        settled = resource.generation

        target = ManagedResource.objects.get(key="homelab-npm-certificate-target")
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

        from application.infrastructure import delivery_targets

        resource = self._certificate()
        self.assertEqual(resource.desired_fingerprint, "")

        advance_dependents(delivery_targets())

        resource.refresh_from_db()
        self.assertTrue(resource.desired_fingerprint)
        self.assertEqual(resource.generation, 1)

    def test_saving_a_target_advances_them_without_a_separate_step(self):
        """Editing the target is the whole action; nothing else has to be run."""

        from application.infrastructure import (
            ManagedResourceCommand,
            save_managed_resource,
        )
        from application.security import cli_principal

        resource = self._certificate()
        advance_dependents(delivery_targets_for_test())
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
        from application.infrastructure import delivery_targets

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

    Every bug this class exists to catch had the same shape: two providers
    supplying one facet, one of them declaring a hook the other did not, and a
    page reporting HQ's own silence as a fact about the world. Found one at a
    time from the outside, each looked like its own defect. They were one.
    """

    def test_a_provider_that_says_where_a_name_points_says_where_it_is_served(self):
        """Answering for a name and routing it are the same knowledge.

        A provider that can say which address a name resolves to can say where
        that name is served -- the address *is* the answer. Declaring the first
        and withholding the second is how a live service came to report "nothing
        supplies this": one DNS provider declared an origin and the other did
        not, so a name carried by the quiet one had no origin at all, no machine,
        and no runtime, while the box serving it sat in HQ's own inventory.
        """

        from .providers import PROVIDERS

        silent = sorted(
            kind
            for kind, provider in PROVIDERS.items()
            if provider.answers is not None and provider.origin is None
        )

        self.assertEqual(
            silent,
            [],
            "these say where a name resolves but not where it is served, so a "
            "name they carry alone will read as unrouted: " + ", ".join(silent),
        )

    def test_every_facet_provider_agrees_on_what_a_hostname_is(self):
        """A facet is a column, and two providers must fill it the same way."""

        from .providers import PROVIDERS

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
        cpanel["install_domains"] = ["quiz.jseverino.net"]

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
        self.assertFalse(
            providers["tls.certificate"]["spec_schema"]["additionalProperties"]
        )
        self.assertIn(
            "install_on",
            providers["tls.certificate"]["spec_schema"]["properties"],
        )

    def test_certificate_contract_normalizes_and_validates_deployments(self):
        spec = resolved_certificate_spec()
        spec["domains"][0] = "JSEVERINO.COM."
        self.assertEqual(
            validate_resolved_certificate(spec)["domains"][0],
            "jseverino.com",
        )

        spec["consumers"][-1]["install_domains"].append("not-covered.example")
        with self.assertRaisesRegex(ValueError, "must be present"):
            validate_resolved_certificate(spec)

    def test_invalid_provider_payload_fails_before_persistence(self):
        result = execute_capability(
            "infrastructure.resource.create",
            {
                "key": "bad-proxy",
                "kind": "npm.proxy_host",
                "spec": {
                    "domain_names": ["dev-hq.jseverino.com"],
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
                    "zone": "jseverino.com",
                    "name": "hq.jseverino.com",
                    "record_type": "A",
                    "content": "192.0.2.1",
                },
            ),
            principal=cli_principal(),
        )
        self.assertTrue(disabled["ok"])
        with self.assertRaisesRegex(PolicyError, "public DNS"):
            save_managed_resource(
                ManagedResourceCommand(
                    key="public-hq",
                    kind="cloudflare.dns_record",
                    spec={
                        "zone": "jseverino.com",
                        "name": "hq.jseverino.com",
                        "record_type": "A",
                        "content": "192.0.2.1",
                    },
                ),
                principal=cli_principal(),
            )


class InfrastructureWebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator",
            password="test-only-password",
        )
        self.client.force_login(self.user)
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(
                key="jseverino-wildcard",
                kind="tls.certificate",
                spec=certificate_spec(),
            ),
            principal=cli_principal(),
        )
        self.resource = ManagedResource.objects.get(key="jseverino-wildcard")

    def test_machine_edit_can_choose_the_dashboard_telemetry_owner(self):
        from control_plane.models import DashboardMachine

        machine = ManagedResource.objects.create(
            key="homelab-server",
            kind="machine",
            spec={
                "name": "homelab-server",
                "role": "Infrastructure host",
                "addresses": ["100.64.0.9"],
            },
        )
        edit_url = reverse("control_plane:edit", kwargs={"key": machine.key})

        page = self.client.get(edit_url)
        response = self.client.post(
            edit_url,
            {
                "name": "homelab-server",
                "role": "Infrastructure host",
                "addresses": "100.64.0.9",
                "enabled": "on",
                "show_on_dashboard": "on",
            },
        )

        self.assertContains(page, "Show on dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DashboardMachine.objects.get().machine, machine)

    def test_detail_shows_active_observation_and_policy_gated_renewal(self):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=89, hours=23)).isoformat(),
            "certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n",
            "consumers": [
                {
                    "consumer": "jseverino-wildcard",
                    "domain": "hq.jseverino.com",
                },
                {
                    "consumer": "jseverino-wildcard",
                    "domain": "sso.jseverino.com",
                },
            ],
        }
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse("control_plane:detail", kwargs={"key": self.resource.key})
        )

        self.assertContains(response, "Automatic")
        self.assertContains(response, "resumes automatically after restarts")
        self.assertContains(response, "Renewal policy")
        self.assertContains(response, "90 days remaining")
        self.assertContains(response, "hq.jseverino.com")
        self.assertContains(response, "sso.jseverino.com")
        self.assertNotContains(response, "BEGIN CERTIFICATE")
        self.assertContains(response, "certificate_available")
        self.assertContains(response, "True")

    def test_a_proxy_host_and_its_upstream_are_distinct_machine_edges(self):
        ManagedResource.objects.create(
            key="homelab-server",
            kind="machine",
            spec={"name": "homelab-server", "addresses": ["100.64.0.9"]},
        )
        ManagedResource.objects.create(
            key="app-server",
            kind="machine",
            spec={"name": "app-server", "addresses": ["100.64.0.10"]},
        )
        ProviderConnection.objects.create(
            connection_ref="an-npm",
            controller_id="homelab-server",
            provider="npm",
            endpoint="https://npm.example.test",
            reaches=["app-server"],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )
        proxy = ManagedResource.objects.create(
            key="an-app-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["app.example.test"],
                "forward_scheme": "http",
                "forward_host": "100.64.0.10",
                "forward_port": 8000,
            },
        )

        response = self.client.get(
            reverse("control_plane:detail", kwargs={"key": proxy.key})
        )

        self.assertEqual(response.context["provider_machine"]["name"], "homelab-server")
        self.assertEqual(response.context["origin_machine"].name, "app-server")
        # Two edges, two sentences. Both said "Runs on" once, which on a
        # container managed by a Portainer one box over printed "Runs on
        # homelab-server" directly above "Runs on sl-cloud-edge-01" -- the same
        # words for the machine the provider is on and the machine the thing is
        # on, wrong one first.
        self.assertContains(response, "Managed through")
        self.assertContains(response, "Forwards to")

    def test_public_certificate_download_never_serves_private_key(self):
        self.resource.status = {
            "certificate_pem": "-----BEGIN PRIVATE KEY-----\nunsafe\n"
        }
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse(
                "control_plane:certificate_download",
                kwargs={"key": self.resource.key},
            )
        )
        self.assertEqual(response.status_code, 500)

        self.resource.status = {
            "certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n"
        }
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse(
                "control_plane:certificate_download",
                kwargs={"key": self.resource.key},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-pem-file")
        self.assertIn("jseverino-wildcard-public.pem", response["Content-Disposition"])


class OperationPolicyTests(TestCase):
    def setUp(self):
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(
                key="jseverino-wildcard",
                kind="tls.certificate",
                spec=certificate_spec(),
            ),
            principal=cli_principal(),
        )
        self.resource = ManagedResource.objects.get(key="jseverino-wildcard")

    def test_a_verb_returns_to_the_page_that_offered_it(self):
        """These forms sit on pages that show the fact the verb answers, and
        they have been sending `next` all along while the view returned to the
        resource record regardless."""

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser("op", password="x" * 20)
        self.client.force_login(user)

        response = self.client.post(
            reverse("control_plane:reconcile", kwargs={"key": self.resource.key}),
            {"next": "/infrastructure/machines/a-host/"},
        )

        self.assertRedirects(
            response,
            "/infrastructure/machines/a-host/",
            fetch_redirect_response=False,
        )

    def test_a_destination_off_this_host_is_refused(self):
        """`next` is operator input, so it goes through the shared check."""

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser("op2", password="x" * 20)
        self.client.force_login(user)

        response = self.client.post(
            reverse("control_plane:reconcile", kwargs={"key": self.resource.key}),
            {"next": "https://example.test/somewhere"},
        )

        self.assertRedirects(
            response,
            reverse("control_plane:detail", kwargs={"key": self.resource.key}),
            fetch_redirect_response=False,
        )

    def test_controller_automatically_queues_due_certificate_renewal(self):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=29)).isoformat()
        }
        self.resource.conditions = [
            {"type": "Ready", "status": True, "reason": "Verified"}
        ]
        self.resource.observed_generation = self.resource.generation
        self.resource.save()

        result = schedule_automatic_operations("a-docker-host")

        operation = OperationRequest.objects.get()
        self.assertEqual(result["scheduled"], [str(operation.id)])
        self.assertEqual(operation.action, OperationRequest.Action.RENEW)
        self.assertEqual(operation.requested_interface, "controller")

    def test_controller_automatically_reconciles_a_new_generation(self):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=89)).isoformat()
        }
        self.resource.conditions = [
            {"type": "Ready", "status": True, "reason": "Verified"}
        ]
        self.resource.observed_generation = self.resource.generation - 1
        self.resource.save()

        schedule_automatic_operations("a-docker-host")

        self.assertEqual(
            OperationRequest.objects.get().action,
            OperationRequest.Action.RECONCILE,
        )

    @patch("application.controller._scheduler_now")
    def test_failed_automatic_renewal_retries_once_on_the_next_day(self, now):
        current = timezone.now()
        now.return_value = current
        self.resource.status = {"not_after": (current + timedelta(days=29)).isoformat()}
        self.resource.observed_generation = self.resource.generation
        self.resource.save()

        schedule_automatic_operations("a-docker-host")
        operation = OperationRequest.objects.get()
        operation.state = OperationRequest.State.FAILED
        operation.save(update_fields=("state", "updated_at"))
        schedule_automatic_operations("a-docker-host")
        self.assertEqual(OperationRequest.objects.count(), 1)

        now.return_value = current + timedelta(days=1)
        schedule_automatic_operations("a-docker-host")
        self.assertEqual(OperationRequest.objects.count(), 2)

    def test_active_controller_capability_queues_renewal_work(self):
        result = request_certificate_renewal(
            OperationCommand(idempotency_key="renew-active"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )

        self.assertTrue(result["queued"])
        self.assertEqual(result["operation"]["action"], "renew")

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_locked_reconcile_capability_cannot_queue_work(self):
        """A domain is the locked capability now that DNS records apply.

        Declaring one records which zones HQ is responsible for. It carries no
        settings, so there is nothing for a reconcile to converge toward -- and
        asking for one must be refused rather than queued for a worker that
        could only fail.
        """

        save_managed_resource(
            ManagedResourceCommand(
                key="a-domain",
                kind="cloudflare.zone",
                spec={"zone": "example.com", "connection_ref": "cf-example"},
            ),
            principal=cli_principal(),
        )

        with self.assertRaisesRegex(PolicyError, "nothing to converge"):
            request_reconcile(
                OperationCommand(idempotency_key="zone-locked"),
                principal=cli_principal(),
                current_key="a-domain",
            )
        self.assertFalse(OperationRequest.objects.exists())

    @patch(
        "application.infrastructure.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_renewal_is_blocked_outside_window(self, _policy):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=45)).isoformat()
        }
        self.resource.save()
        with self.assertRaisesRegex(PolicyError, "Renewal opens"):
            request_certificate_renewal(
                OperationCommand(idempotency_key="renew-too-early"),
                principal=cli_principal(),
                current_key=self.resource.key,
            )

    @patch(
        "application.infrastructure.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_renewal_is_allowed_for_drift_and_idempotent(self, _policy):
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=45)).isoformat()
        }
        self.resource.conditions = [
            {"type": "Drifted", "status": True, "reason": "NPMStale"}
        ]
        self.resource.save()
        first = request_certificate_renewal(
            OperationCommand(idempotency_key="renew-drift"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        second = request_certificate_renewal(
            OperationCommand(idempotency_key="renew-drift"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])
        self.assertEqual(OperationRequest.objects.count(), 1)

    @override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=False)
    def test_mcp_infrastructure_is_fail_closed(self):
        result = execute_capability(
            "certificate.renew",
            {"idempotency_key": "mcp-denied"},
            principal=mcp_principal(),
            target=self.resource.key,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")

    @patch(
        "application.infrastructure.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_controller_claim_and_report_updates_observed_state(self, _policy):
        queued = request_reconcile(
            OperationCommand(idempotency_key="reconcile-once"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        claimed = StringIO()
        call_command(
            "infrastructure_controller",
            "claim",
            controller_id="homelab-controller",
            stdout=claimed,
        )
        claim_payload = json.loads(claimed.getvalue())
        self.assertEqual(claim_payload["operation"]["id"], queued["operation"]["id"])
        self.assertEqual(
            claim_payload["resource"]["spec"]["certificate_name"],
            "jseverino",
        )

        reported = StringIO()
        call_command(
            "infrastructure_controller",
            "report",
            controller_id="homelab-controller",
            operation=queued["operation"]["id"],
            payload=json.dumps(
                {
                    "success": True,
                    "observed_generation": self.resource.generation,
                    "status": {
                        "not_after": (timezone.now() + timedelta(days=89)).isoformat()
                    },
                    "conditions": [
                        {"type": "Ready", "status": True, "reason": "Verified"}
                    ],
                    "message": "All consumers verified.",
                }
            ),
            stdout=reported,
        )
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.observed_generation, self.resource.generation)
        self.assertEqual(
            json.loads(reported.getvalue())["operation"]["state"], "succeeded"
        )


class DeliveryTargetConfirmationTests(TestCase):
    """Verifying a certificate is the only thing that ever sees its targets.

    Nothing sweeps a delivery target, so its "last confirmed" said *never* for
    as long as it existed -- while every reconcile was opening a connection to
    it, reading back what it served and matching the fingerprint. The evidence
    was arriving under the certificate's name and being dropped.
    """

    def _report(self, consumers):
        certificate = ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )
        operation = OperationRequest.objects.create(
            resource=certificate,
            action=OperationRequest.Action.RECONCILE,
            idempotency_key="verify-once",
            state=OperationRequest.State.CLAIMED,
            claimed_by="homelab-controller",
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
            controller_id="homelab-controller",
        )

    def _confirmed(self, connection_ref):
        return ManagedResource.objects.get(
            key=f"{connection_ref}-certificate-target"
        ).last_observed_at

    def test_a_target_the_certificate_was_verified_at_is_confirmed(self):
        declare_targets()

        self._report([{"consumer": "edge-caddy", "domain": "health.example.com"}])

        self.assertIsNotNone(self._confirmed("edge"))

    def test_a_target_nothing_reached_is_left_unconfirmed(self):
        """Confirming all three off one observation would be inventing evidence."""

        declare_targets()

        self._report([{"consumer": "edge-caddy", "domain": "health.example.com"}])

        self.assertIsNone(self._confirmed("a-shared-host"))

    def test_a_report_with_no_observations_confirms_nothing(self):
        declare_targets()

        self._report([])

        self.assertIsNone(self._confirmed("edge"))


class InfrastructureViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="joe", password="test-password"
        )
        self.resource = ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
            status={"certificate_pem": "PUBLIC CERTIFICATE ONLY"},
        )
        self.client.force_login(self.user)

    def test_private_dashboard_and_public_certificate_download(self):
        dashboard = self.client.get(reverse("control_plane:list"))
        self.assertContains(dashboard, "jseverino-wildcard")

        download = self.client.get(
            reverse("control_plane:certificate_download", args=[self.resource.key])
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"PUBLIC CERTIFICATE ONLY")
        self.assertNotIn(b"PRIVATE", download.content)

        report = self.client.get(
            reverse("control_plane:report_download", args=[self.resource.key])
        )
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["resource"]["key"], self.resource.key)

    def test_findings_page_explains_the_claim_and_its_evidence(self):
        response = self.client.get(reverse("control_plane:findings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Findings")
        self.assertContains(response, "Nothing has ever observed tls.certificate")
        self.assertContains(response, "Records of this kind")
        self.assertContains(response, reverse("action_items"))

    def test_findings_render_only_offers_the_projection_authorized(self):
        from application.action_links import ActionLink
        from application.findings import Finding
        from application.topology import Topology, TopologyNode

        subject = TopologyNode(
            "controller:one",
            "controller",
            "HQ dev",
            "Controller",
        )
        finding = Finding(
            "controller-sweep-stale",
            subject.id,
            "HQ dev stopped confirming two kinds",
            "serious",
            "The graph proves one shared cause.",
            offers=(ActionLink("open", "Open connections", "read", "/connections/"),),
            investigations=(
                ActionLink("impact", "Trace impact", "read", "/topology/?trace"),
            ),
        )
        with (
            patch(
                "control_plane.views.derive_topology",
                return_value=Topology((subject,), ()),
            ),
            patch("control_plane.views.derive_findings", return_value=(finding,)),
        ):
            response = self.client.get(reverse("control_plane:findings"))

        self.assertContains(response, "What HQ can do now")
        self.assertContains(response, "Open connections")
        self.assertContains(response, 'href="/topology/?trace"')
        self.assertContains(response, "Trace impact")

    def test_legacy_operation_evidence_is_not_mislabeled_as_a_mismatch(self):
        OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.SUCCEEDED,
            requested_actor="joe",
            requested_interface="web",
            idempotency_key="legacy-success",
            input={"generation": self.resource.generation},
            result={
                "message": "Certificate consumers already match the managed lineage.",
                "status": {
                    "consumers": [
                        {
                            "consumer": "npm",
                            "domain": "hq.example.test",
                            "fingerprint_sha256": "legacy",
                        }
                    ]
                },
            },
        )

        detail = self.client.get(
            reverse("control_plane:detail", args=[self.resource.key])
        )

        self.assertContains(detail, "observed (legacy result)")
        self.assertNotContains(detail, "did not match")

    def test_failed_operation_renders_guidance_and_expected_observed_evidence(self):
        OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.FAILED,
            requested_actor="homelab-controller",
            requested_interface="controller",
            idempotency_key="structured-failure",
            result={
                "message": "Verification did not converge.",
                "conditions": [
                    {
                        "type": "Degraded",
                        "status": True,
                        "reason": "VerificationFailed",
                        "message": "One consumer serves the previous certificate.",
                    }
                ],
                "status": {
                    "expected_fingerprint_sha256": "expected-fingerprint",
                    "consumers": [
                        {
                            "consumer": "npm",
                            "domain": "hq.example.test",
                            "fingerprint_sha256": "observed-fingerprint",
                            "matches_expected": False,
                        }
                    ],
                },
            },
        )

        detail = self.client.get(
            reverse("control_plane:detail", args=[self.resource.key])
        )

        self.assertContains(detail, "One consumer serves the previous certificate.")
        self.assertContains(detail, "Provider reason:")
        self.assertContains(detail, "VerificationFailed")
        self.assertContains(detail, "1 affected target")
        self.assertContains(detail, "Expected")
        self.assertContains(detail, "observed")
        self.assertContains(detail, "Raw controller result")

    def test_controller_rejects_secret_material_in_status(self):
        operation = OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.CLAIMED,
            requested_actor="operator",
            requested_interface="cli",
            idempotency_key="secret-report",
            input={"generation": self.resource.generation},
            claimed_by="controller",
            claimed_at=timezone.now(),
        )
        with self.assertRaisesRegex(ValueError, "secret-bearing"):
            report_operation(
                str(operation.id),
                ControllerReport(
                    success=True,
                    observed_generation=self.resource.generation,
                    status={"api_token": "must-never-enter-hq"},
                ),
                controller_id="controller",
            )
        with self.assertRaisesRegex(ValueError, "private-key material"):
            report_operation(
                str(operation.id),
                ControllerReport(
                    success=True,
                    observed_generation=self.resource.generation,
                    status={"certificate_pem": "-----BEGIN PRIVATE KEY-----\nnope"},
                ),
                controller_id="controller",
            )

    def test_failed_controller_report_preserves_last_verified_status(self):
        verified = {
            "not_after": "2026-10-23T00:00:00+00:00",
            "certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n",
        }
        self.resource.status = verified
        self.resource.save(update_fields=("status",))
        operation = OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RENEW,
            state=OperationRequest.State.CLAIMED,
            requested_actor="operator",
            requested_interface="cli",
            idempotency_key="preserve-verified-status",
            input={"generation": self.resource.generation},
            claimed_by="controller",
            claimed_at=timezone.now(),
        )

        report_operation(
            str(operation.id),
            ControllerReport(
                success=False,
                observed_generation=self.resource.generation,
                status={"expected_fingerprint_sha256": "new"},
                conditions=[
                    {
                        "type": "Degraded",
                        "status": True,
                        "reason": "ProviderError",
                        "message": "One consumer did not converge.",
                    }
                ],
                message="One consumer did not converge.",
            ),
            controller_id="controller",
        )

        self.resource.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual(self.resource.status, verified)
        self.assertEqual(
            operation.result["status"]["expected_fingerprint_sha256"], "new"
        )


class DnsRecordReadoutTests(TestCase):
    """A record that matches the world must not report drift against itself."""

    def test_a_matching_record_reports_no_drift(self):
        from control_plane.providers import _dns_record_readout

        spec = {
            "zone": "example.com",
            "name": "example.com",
            "record_type": "CNAME",
            "content": "example.pages.dev",
            "priority": None,
            "proxied": True,
            "ttl": 1,
        }
        status = {**spec, "record_id": "abc"}
        label, desired, observed = _dns_record_readout(spec, status)[0]
        self.assertEqual(desired, observed)

    def test_a_changed_record_still_reports_drift(self):
        from control_plane.providers import _dns_record_readout

        spec = {"record_type": "CNAME", "content": "new.pages.dev", "priority": None}
        status = {"record_type": "CNAME", "content": "old.pages.dev", "priority": None}
        _, desired, observed = _dns_record_readout(spec, status)[0]
        self.assertNotEqual(desired, observed)

    def test_priority_is_compared_on_both_sides(self):
        from control_plane.providers import _dns_record_readout

        spec = {"record_type": "MX", "content": "mx.example.com", "priority": 10}
        status = {**spec}
        _, desired, observed = _dns_record_readout(spec, status)[0]
        self.assertEqual(desired, observed)
        self.assertIn("10", desired)

    def test_an_unobserved_record_reports_nothing_rather_than_drift(self):
        from control_plane.providers import _dns_record_readout

        spec = {"record_type": "A", "content": "192.0.2.1", "priority": None}
        _, desired, observed = _dns_record_readout(spec, {})[0]
        self.assertEqual(observed, "")
        self.assertTrue(desired)


class QueueHeadTests(TestCase):
    """One resource HQ cannot describe must not stop every other one.

    The queue is ordered by age and a claim is atomic, so an operation whose
    contract could not be built rolled the claim back and stayed exactly where
    it was. Every poll after it hit the same one, and nothing else -- no DNS, no
    proxy hosts, no renewals -- was ever claimed again.
    """

    def setUp(self):
        from application.controller import claim_next_operation

        self.claim = claim_next_operation
        declare_targets()
        self.certificate = ManagedResource.objects.create(
            key="jseverino-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
        )
        self.rewrite = ManagedResource.objects.create(
            key="hq-dns",
            kind="adguard.rewrite",
            spec={"domain": "hq.example.com", "answer": "10.0.0.10"},
        )

    def queue(self, resource, key):
        return OperationRequest.objects.create(
            resource=resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.QUEUED,
            requested_interface="cli",
            idempotency_key=key,
        )

    def test_work_behind_an_unresolvable_resource_is_still_claimed(self):
        broken = self.queue(self.certificate, "broken")
        wanted = self.queue(self.rewrite, "wanted")
        ManagedResource.objects.filter(kind="tls.delivery_target").delete()

        claimed = self.claim(
            "a-controller",
            capabilities=(
                ("adguard.rewrite", "reconcile"),
                ("tls.certificate", "reconcile"),
            ),
        )

        self.assertEqual(claimed["operation"]["id"], str(wanted.id))
        broken.refresh_from_db()
        self.assertEqual(broken.state, OperationRequest.State.FAILED)

    def test_the_failure_says_what_could_not_be_resolved(self):
        broken = self.queue(self.certificate, "broken")
        ManagedResource.objects.filter(kind="tls.delivery_target").delete()

        self.claim("a-controller", capabilities=(("tls.certificate", "reconcile"),))

        broken.refresh_from_db()
        self.assertIn("receives a certificate", broken.result["message"])

    def test_a_resolvable_queue_is_untouched(self):
        wanted = self.queue(self.certificate, "wanted")

        claimed = self.claim(
            "a-controller", capabilities=(("tls.certificate", "reconcile"),)
        )

        self.assertEqual(claimed["operation"]["id"], str(wanted.id))
        self.assertIn("resource", claimed)

    def test_removing_a_target_stops_the_certificate_reporting_itself_in_sync(self):
        """Removing one is as much a change as editing one."""

        from application.infrastructure import OperationCommand, request_removal
        from application.security import cli_principal

        self.certificate.desired_fingerprint = "settled"
        self.certificate.observed_generation = self.certificate.generation
        self.certificate.save()

        request_removal(
            OperationCommand(idempotency_key="forget-it"),
            principal=cli_principal(),
            current_key="edge-certificate-target",
        )

        self.certificate.refresh_from_db()
        self.assertGreater(
            self.certificate.generation, self.certificate.observed_generation
        )


class ReadoutsSayNothingBlankTests(TestCase):
    """A readout row must be able to hold a value.

    Two of these shipped and neither was noticed from the code: a zone printed
    a record count and an MX, SPF, DMARC and CAA summary from five `status`
    keys nothing has ever written, and a container printed a state from a key a
    sweep does not store. Ten container pages and four domain pages, every one
    of them an em dash under a label promising an observation.

    Blank on the resource in front of you is normal -- a certificate that has
    not been reconciled yet has no expiry. Blank on *every* resource of a kind,
    across desired and observed both, means the row reads a key with no writer.
    """

    def _rows(self, kind, spec, status=None):
        from .providers import PROVIDERS

        provider = PROVIDERS[kind]
        return provider.readout(spec, status or {}) if provider.readout else ()

    def test_a_container_does_not_print_a_state_nothing_records(self):
        """A sweep confirms a container by its identity, which holds no state."""

        labels = [
            label
            for label, _, _ in self._rows(
                "portainer.container",
                {"connection_ref": "a-portainer", "host": "a-box", "name": "app"},
            )
        ]

        self.assertNotIn("State", labels)

    def test_a_zone_does_not_print_summaries_it_cannot_derive(self):
        """A readout gets a spec and a status and no database.

        The domain page derives all of these from the records HQ sweeps, and
        says more besides. This one could only ever print the placeholder where
        the derivation would have gone.
        """

        labels = [
            label
            for label, _, _ in self._rows(
                "cloudflare.zone", {"zone": "example.com", "connection_ref": "a-dns"}
            )
        ]

        for absent in ("Records", "Mail (MX)", "SPF", "DMARC"):
            self.assertNotIn(absent, labels)

    # No generic "every row can hold a value" test here. Fed a synthetic status
    # that answers every key, four rows that are populated in production came
    # back blank -- a certificate's consumers and the tailnet policy's grants,
    # groups and tests all read lists, and a fixture cannot fake a list without
    # knowing its shape. The sound version of this check needs the real estate,
    # so it is an audit run against it rather than a test that would cry wolf
    # on every future provider that reads a collection.
