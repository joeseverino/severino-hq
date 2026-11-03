"""The infrastructure pages: what they show, what they offer, and how they read."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pydantic import ValidationError

from hq.platform.application.action_links import ActionLink
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.controller import ControllerReport, report_operation
from hq.platform.application.finding_model import Finding
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.markup import actions, hrefs, select, text
from hq.platform.application.tests.writers import form_keyed
from hq.platform.application.topology_model import TopologyNode

from ..models import DashboardMachine, ManagedResource, OperationRequest, ProviderConnection
from ..provider_adapters.cloudflare import _dns_record_readout
from ..providers import PROVIDERS, validate_spec
from . import bridge_client
from .certificate_cases import certificate_spec, declare_targets


class InfrastructureWebTests(TestCase):
    def setUp(self):
        managing_everything()
        self.user = get_user_model().objects.create_user(
            username="operator",
            password="test-only-password",
        )
        self.client.force_login(self.user)
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(
                key="example-wildcard",
                kind="tls.certificate",
                spec=certificate_spec(),
            ),
            principal=cli_principal(),
        )
        self.resource = ManagedResource.objects.get(key="example-wildcard")

    def test_machine_edit_can_choose_the_dashboard_telemetry_owner(self):
        machine = ManagedResource.objects.create(
            key="example-host",
            kind="machine",
            spec={
                "name": "example-host",
                "role": "Infrastructure host",
                "addresses": ["100.64.0.9"],
            },
        )
        edit_url = reverse("control_plane:edit", kwargs={"key": machine.key})

        page = self.client.get(edit_url)
        response = self.client.post(
            edit_url,
            {
                "name": "example-host",
                "role": "Infrastructure host",
                "addresses": "100.64.0.9",
                "enabled": "on",
                "show_on_dashboard": "on",
                **form_keyed(),
            },
        )

        self.assertTrue(select(page, "name", "show_on_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DashboardMachine.objects.get().machine, machine)

    def test_detail_shows_active_observation_and_policy_gated_renewal(self):
        managing_everything()
        self.resource.status = {
            "not_after": (timezone.now() + timedelta(days=89, hours=23)).isoformat(),
            "certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n",
            "consumers": [
                {
                    "consumer": "example-wildcard",
                    "domain": "hq.example.com",
                },
                {
                    "consumer": "example-wildcard",
                    "domain": "sso.example.com",
                },
            ],
        }
        self.resource.save(update_fields=("status",))
        response = self.client.get(reverse("control_plane:detail", kwargs={"key": self.resource.key}))

        # One status line: this certificate's first change has not been applied.
        self.assertEqual(
            [line.attrs["data-record-status"] for line in select(response, "data-record-status")], ["pending"]
        )
        shown = text(response)
        self.assertIn("Renews automatically from", shown)
        self.assertIn("hq.example.com", shown)
        self.assertIn("sso.example.com", shown)
        # The public certificate is offered as a download, never printed.
        self.assertNotIn("BEGIN CERTIFICATE", shown)
        self.assertTrue(json.loads(response.context["diagnostic_status"])["certificate_available"])
        self.assertIn(reverse("control_plane:certificate_download", args=[self.resource.key]), hrefs(response))

    def test_a_proxy_host_and_its_upstream_are_distinct_machine_edges(self):
        ManagedResource.objects.create(
            key="example-host",
            kind="machine",
            spec={"name": "example-host", "addresses": ["100.64.0.9"]},
        )
        ManagedResource.objects.create(
            key="app-server",
            kind="machine",
            spec={"name": "app-server", "addresses": ["100.64.0.10"]},
        )
        ProviderConnection.objects.create(
            connection_ref="an-npm",
            controller_id="example-host",
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

        response = self.client.get(reverse("control_plane:detail", kwargs={"key": proxy.key}))

        self.assertEqual(response.context["provider_machine"]["name"], "example-host")
        self.assertEqual(response.context["origin_machine"].name, "app-server")
        # Two edges: the machine its provider runs on and the one it forwards to.
        links = hrefs(response)
        for name in ("example-host", "app-server"):
            self.assertIn(reverse("control_plane:machine", kwargs={"name": name}), links)

    def test_public_certificate_download_never_serves_private_key(self):
        self.resource.status = {"certificate_pem": "-----BEGIN PRIVATE KEY-----\nunsafe\n"}
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse(
                "control_plane:certificate_download",
                kwargs={"key": self.resource.key},
            )
        )
        self.assertEqual(response.status_code, 500)

        self.resource.status = {"certificate_pem": "-----BEGIN CERTIFICATE-----\npublic\n"}
        self.resource.save(update_fields=("status",))
        response = self.client.get(
            reverse(
                "control_plane:certificate_download",
                kwargs={"key": self.resource.key},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-pem-file")
        self.assertIn("example-wildcard-public.pem", response["Content-Disposition"])


class InfrastructureViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="joe", password="test-password")
        self.resource = ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec=certificate_spec(),
            status={"certificate_pem": "PUBLIC CERTIFICATE ONLY"},
        )
        self.client.force_login(self.user)

    def test_a_row_does_not_repeat_under_its_name_what_its_name_says(self):
        ManagedResource.objects.create(
            key="example-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["app.example.com"],
                "forward_scheme": "http",
                "forward_host": "10.0.0.10",
                "forward_port": 8000,
            },
        )

        shown = text(self.client.get(reverse("control_plane:list")))

        self.assertIn("10.0.0.10:8000", shown)
        self.assertNotIn("http://10.0.0.10:8000", shown)

    def test_private_dashboard_and_public_certificate_download(self):
        dashboard = self.client.get(reverse("control_plane:list"))
        self.assertIn(self.resource.get_absolute_url(), hrefs(dashboard))

        download = self.client.get(reverse("control_plane:certificate_download", args=[self.resource.key]))
        self.assertEqual(download.content, b"PUBLIC CERTIFICATE ONLY")

        report = self.client.get(reverse("control_plane:report_download", args=[self.resource.key]))
        self.assertEqual(report.json()["resource"]["key"], self.resource.key)

    def test_findings_page_explains_the_claim_and_its_evidence(self):
        # A kind a sweep actually visits. The certificate in `setUp` is not
        # one: nothing collects certificates, so measuring them against the
        # sweep interval says only that they are not swept, which this rule
        # does not claim.
        ManagedResource.objects.create(
            key="unswept-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "unswept.example", "answer": "10.0.0.9"},
        )

        response = self.client.get(reverse("control_plane:findings"))

        self.assertIn("kind-never-swept", [rule.name for rule in response.context["finding_rules"]])
        self.assertGreaterEqual(response.context["finding_counts"].get("serious", 0), 1)
        links = hrefs(response)
        self.assertIn(f"{reverse('control_plane:findings')}?rule=kind-never-swept", links)
        self.assertIn(reverse("action_items"), links)

    def test_findings_render_only_offers_the_projection_authorized(self):
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
            investigations=(ActionLink("impact", "Trace impact", "read", "/topology/?trace"),),
        )
        with patch("hq.domains.control_plane.finding_views.estate_findings", return_value=(finding,)):
            response = self.client.get(reverse("control_plane:findings"))

        # Pages to look into it are links, never buttons: the projection's own.
        for url in ("/connections/", "/topology/?trace"):
            self.assertIn(url, hrefs(response))
            self.assertNotIn(url, actions(response))

    def test_legacy_operation_evidence_is_not_mislabeled_as_a_mismatch(self):
        OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.SUCCEEDED,
            completed_at=timezone.now(),
            requested_actor="joe",
            requested_interface="web",
            idempotency_key="legacy-success",
            input={"generation": self.resource.generation},
            result={
                "message": "Every site already serves this certificate.",
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

        detail = self.client.get(reverse("control_plane:detail", args=[self.resource.key]))

        (operation,) = detail.context["operations"]
        self.assertFalse(operation["affected"])

    def test_failed_operation_renders_guidance_and_expected_observed_evidence(self):
        OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.FAILED,
            completed_at=timezone.now(),
            requested_actor="example-controller",
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

        detail = self.client.get(reverse("control_plane:detail", args=[self.resource.key]))

        (operation,) = detail.context["operations"]
        self.assertEqual(
            [(item["consumer"], item["domain"]) for item in operation["affected"]], [("npm", "hq.example.test")]
        )
        self.assertTrue(operation["raw_result"])
        shown = text(detail)
        self.assertIn("One consumer serves the previous certificate.", shown)
        self.assertIn("1 name serves a different certificate", shown)

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
            lease_expires_at=timezone.now() + timedelta(minutes=5),
        )
        status = {"api_token": "must-never-enter-hq", "certificate_pem": "-----BEGIN PRIVATE KEY-----\nnope"}
        with self.assertLogs("severino.bridge", "WARNING"):
            bridge_client.call(
                "report",
                {"success": False, "observed_generation": self.resource.generation, "status": status},
                operation=str(operation.id),
                controller_id="controller",
            )
        operation.refresh_from_db()
        self.assertEqual(operation.state, OperationRequest.State.FAILED)
        self.assertEqual(operation.result["status"], {"api_token": "[redacted]", "certificate_pem": "[redacted]"})

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
            lease_expires_at=timezone.now() + timedelta(minutes=5),
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
        self.assertEqual(operation.result["status"]["expected_fingerprint_sha256"], "new")


class DnsRecordReadoutTests(TestCase):
    """A record that matches the world must not report drift against itself."""

    def test_a_matching_record_reports_no_drift(self):
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
        _label, desired, observed = _dns_record_readout(spec, status)[0]
        self.assertEqual(desired, observed)

    def test_a_changed_content_or_priority_reports_drift(self):
        for field, desired_value, observed_value in (
            ("content", "new.pages.dev", "old.pages.dev"),
            ("priority", 10, 20),
        ):
            with self.subTest(field=field):
                spec = {"record_type": "MX", "content": "mx.example.com", "priority": 10, field: desired_value}
                status = {**spec, field: observed_value}
                _, desired, observed = _dns_record_readout(spec, status)[0]
                self.assertNotEqual(desired, observed)

    def test_an_unobserved_record_reports_nothing_rather_than_drift(self):
        spec = {"record_type": "A", "content": "192.0.2.1", "priority": None}
        _, desired, observed = _dns_record_readout(spec, {})[0]
        self.assertEqual(observed, "")
        self.assertTrue(desired)


class ReadoutsSayNothingBlankTests(TestCase):
    """A readout row must be able to hold a value.

    A row reading a `status` key nothing writes is an em dash on every page of
    its kind, under a label promising an observation, and nothing in the code
    shows it.

    Blank on the resource in front of you is normal: a certificate that has
    not been reconciled yet has no expiry. Blank on *every* resource of a kind,
    across desired and observed both, means the row reads a key with no writer.
    """

    def _rows(self, kind, spec, status=None):
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
            label for label, _, _ in self._rows("cloudflare.zone", {"zone": "example.com", "connection_ref": "a-dns"})
        ]

        for absent in ("Records", "Mail (MX)", "SPF", "DMARC"):
            self.assertNotIn(absent, labels)


class MachineNamesRouteTests(TestCase):
    """A machine's name is a path segment of its page."""

    def test_a_name_with_a_slash_is_refused_and_a_plain_one_accepted(self):
        with self.assertRaises(ValidationError):
            validate_spec("machine", {"name": "a/b"})
        self.assertEqual(validate_spec("machine", {"name": "a-box"})["name"], "a-box")

    def test_a_routable_name_gets_the_machine_page_and_a_stored_slash_the_detail_page(self):
        for name, page in (
            ("a-box", reverse("control_plane:machine", kwargs={"name": "a-box"})),
            ("a/b", reverse("control_plane:detail", kwargs={"key": "a-box"})),
        ):
            with self.subTest(name=name):
                resource = ManagedResource(key="a-box", kind="machine", spec={"name": name})
                self.assertEqual(resource.get_absolute_url(), page)
