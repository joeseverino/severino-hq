"""Which operations may be asked for, and the order the controller claims them in."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.controller import claim_next_operation, schedule_automatic_operations
from hq.platform.application.derivation_testing import frozen
from hq.platform.application.infrastructure import ManagedResourceCommand, save_managed_resource
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.record_errors import PolicyError
from hq.platform.application.resource_operations import (
    OperationCommand,
    request_certificate_renewal,
    request_reconcile,
    request_removal,
)
from hq.platform.application.security import MCP_INTERFACE, agent_principal, cli_principal

from ..models import ManagedResource, OperationRequest
from ..providers import PROVIDERS
from . import bridge_client
from .certificate_cases import certificate_spec, declare_targets


class OperationPolicyTests(TestCase):
    def setUp(self):
        managing_everything()
        declare_targets()
        save_managed_resource(
            ManagedResourceCommand(key="example-wildcard", kind="tls.certificate", spec=certificate_spec()),
            principal=cli_principal(),
        )
        OperationRequest.objects.all().delete()  # applied, so the reconcile its save queued is done
        self.resource = ManagedResource.objects.get(key="example-wildcard")

    def test_a_verb_returns_to_the_page_that_offered_it(self):
        """These forms sit on pages that show the fact the verb answers, and
        they have been sending `next` all along while the view returned to the
        resource record regardless."""

        user = get_user_model().objects.create_superuser("op", password="x" * 20)
        self.client.force_login(user)

        response = self.client.post(
            reverse("control_plane:reconcile", kwargs={"key": self.resource.key}),
            form_keyed({"next": "/infrastructure/machines/a-host/"}),
        )

        self.assertRedirects(
            response,
            "/infrastructure/machines/a-host/",
            fetch_redirect_response=False,
        )

    def test_a_destination_off_this_host_is_refused(self):
        """`next` is operator input, so it goes through the shared check."""

        user = get_user_model().objects.create_superuser("op2", password="x" * 20)
        self.client.force_login(user)

        response = self.client.post(
            reverse("control_plane:reconcile", kwargs={"key": self.resource.key}),
            form_keyed({"next": "https://example.test/somewhere"}),
        )

        self.assertRedirects(
            response,
            reverse("control_plane:detail", kwargs={"key": self.resource.key}),
            fetch_redirect_response=False,
        )

    def test_controller_automatically_queues_due_certificate_renewal(self):
        self.resource.status = {"not_after": (timezone.now() + timedelta(days=29)).isoformat()}
        self.resource.conditions = [{"type": "Ready", "status": True, "reason": "Verified"}]
        self.resource.observed_generation = self.resource.generation
        self.resource.save()

        result = schedule_automatic_operations("a-docker-host")

        operation = OperationRequest.objects.get()
        self.assertEqual(result["scheduled"], [str(operation.id)])
        self.assertEqual(operation.action, OperationRequest.Action.RENEW)
        self.assertEqual(operation.requested_interface, "controller")

    def test_controller_automatically_reconciles_a_new_generation(self):
        self.resource.status = {"not_after": (timezone.now() + timedelta(days=89)).isoformat()}
        self.resource.conditions = [{"type": "Ready", "status": True, "reason": "Verified"}]
        self.resource.observed_generation = self.resource.generation - 1
        self.resource.save()

        schedule_automatic_operations("a-docker-host")

        self.assertEqual(
            OperationRequest.objects.get().action,
            OperationRequest.Action.RECONCILE,
        )

    def test_failed_automatic_renewal_retries_once_on_the_next_day(self):
        current = timezone.now()
        self.resource.status = {"not_after": (current + timedelta(days=29)).isoformat()}
        self.resource.observed_generation = self.resource.generation
        self.resource.save()

        with frozen(current):
            schedule_automatic_operations("a-docker-host")
            operation = OperationRequest.objects.get()
            operation.state = OperationRequest.State.FAILED
            operation.completed_at = timezone.now()
            operation.save(update_fields=("state", "completed_at", "updated_at"))
            schedule_automatic_operations("a-docker-host")
        self.assertEqual(OperationRequest.objects.count(), 1)

        with frozen(current + timedelta(days=1)):
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
        """A domain is the locked capability; its DNS records are what apply.

        Declaring one records which zones HQ is responsible for. It carries no
        settings, so there is nothing for a reconcile to converge toward, and
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

        with self.assertRaisesMessage(PolicyError, PROVIDERS["cloudflare.zone"].actions["reconcile"].reason):
            request_reconcile(
                OperationCommand(idempotency_key="zone-locked"),
                principal=cli_principal(),
                current_key="a-domain",
            )
        self.assertFalse(OperationRequest.objects.exists())

    @patch(
        "hq.platform.application.resource_operations.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_renewal_is_blocked_outside_window(self, _policy):
        self.resource.status = {"not_after": (timezone.now() + timedelta(days=45)).isoformat()}
        self.resource.save()
        with self.assertRaisesRegex(PolicyError, "Renews automatically from"):
            request_certificate_renewal(
                OperationCommand(idempotency_key="renew-too-early"),
                principal=cli_principal(),
                current_key=self.resource.key,
            )

    @patch(
        "hq.platform.application.resource_operations.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_renewal_is_allowed_for_drift_and_idempotent(self, _policy):
        self.resource.status = {"not_after": (timezone.now() + timedelta(days=45)).isoformat()}
        self.resource.conditions = [{"type": "Drifted", "status": True, "reason": "NPMStale"}]
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

    @patch(
        "hq.platform.application.resource_operations.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_a_finished_operations_key_replays_it_and_refuses_another_resource(self, _policy):
        first = request_reconcile(
            OperationCommand(idempotency_key="finished-once"), principal=cli_principal(), current_key=self.resource.key
        )
        OperationRequest.objects.filter(pk=first["operation"]["id"]).update(
            state=OperationRequest.State.SUCCEEDED, completed_at=timezone.now()
        )

        again = request_reconcile(
            OperationCommand(idempotency_key="finished-once"), principal=cli_principal(), current_key=self.resource.key
        )
        self.assertFalse(again["queued"])
        self.assertEqual(again["operation"]["id"], first["operation"]["id"])

        save_managed_resource(
            ManagedResourceCommand(key="other-wildcard", kind="tls.certificate", spec=certificate_spec()),
            principal=cli_principal(),
        )
        OperationRequest.objects.filter(resource__key="other-wildcard").delete()  # applied, as above
        with self.assertRaisesMessage(PolicyError, "already used by another operation"):
            request_reconcile(
                OperationCommand(idempotency_key="finished-once"),
                principal=cli_principal(),
                current_key="other-wildcard",
            )
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_an_agent_not_granted_infrastructure_is_refused_a_renewal(self):
        result = execute_capability(
            "certificate.renew",
            {"idempotency_key": "mcp-denied"},
            principal=agent_principal("example-agent", MCP_INTERFACE, frozenset({"read"})),
            target=self.resource.key,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "insufficient_scope")

    @patch(
        "hq.platform.application.resource_operations.controller_action_policy",
        return_value=(True, "active"),
    )
    def test_controller_claim_and_report_updates_observed_state(self, _policy):
        queued = request_reconcile(
            OperationCommand(idempotency_key="reconcile-once"),
            principal=cli_principal(),
            current_key=self.resource.key,
        )
        claim_payload = bridge_client.call(
            "claim", controller_id="example-controller", capability=["tls.certificate:reconcile"]
        )
        self.assertEqual(claim_payload["operation"]["id"], queued["operation"]["id"])
        self.assertEqual(
            claim_payload["resource"]["spec"]["certificate_name"],
            "example",
        )

        reported = bridge_client.call(
            "report",
            {
                "success": True,
                "observed_generation": self.resource.generation,
                "status": {"not_after": (timezone.now() + timedelta(days=89)).isoformat()},
                "conditions": [{"type": "Ready", "status": True, "reason": "Verified", "message": ""}],
                "message": "All consumers verified.",
            },
            controller_id="example-controller",
            operation=queued["operation"]["id"],
        )
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.observed_generation, self.resource.generation)
        self.assertEqual(reported["operation"]["state"], "succeeded")


class QueueHeadTests(TestCase):
    """One resource HQ cannot describe must not stop every other one.

    The queue is ordered by age and a claim is atomic, so an operation whose
    contract cannot be built and whose claim rolls back stays at the head: every
    later poll hits it, and nothing else is ever claimed.
    """

    def setUp(self):
        self.claim = claim_next_operation
        declare_targets()
        self.certificate = ManagedResource.objects.create(
            key="example-wildcard",
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
        self.assertIn("is not a certificate target", broken.result["message"])

    def test_a_resolvable_queue_is_untouched(self):
        wanted = self.queue(self.certificate, "wanted")

        claimed = self.claim("a-controller", capabilities=(("tls.certificate", "reconcile"),))

        self.assertEqual(claimed["operation"]["id"], str(wanted.id))
        self.assertIn("resource", claimed)

    def test_removing_a_target_stops_the_certificate_reporting_itself_in_sync(self):
        """Removing one is as much a change as editing one."""

        self.certificate.desired_fingerprint = "settled"
        self.certificate.observed_generation = self.certificate.generation
        self.certificate.save()

        request_removal(
            OperationCommand(idempotency_key="forget-it"),
            principal=cli_principal(),
            current_key="edge-certificate-target",
        )

        self.certificate.refresh_from_db()
        self.assertGreater(self.certificate.generation, self.certificate.observed_generation)
