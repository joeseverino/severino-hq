"""What HQ offers to do with a resource follows whether its connection manages."""

from __future__ import annotations

from django.test import TestCase

from control_plane.models import ManagedResource

from .adoption_testing import connection
from .resource_capabilities import OBSERVES_ONLY, resource_capabilities


class CapabilitiesFollowTheConnectionTests(TestCase):
    def _rewrite(self):
        return ManagedResource.objects.create(
            key="app-rewrite", kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"}, enabled=True,
        )

    def test_an_observing_connection_offers_nothing_it_cannot_do(self):
        connection("adguard", manages=False)

        found = resource_capabilities(self._rewrite())

        self.assertFalse(found.actions["reconcile"].enabled)
        self.assertEqual(found.actions["reconcile"].reason, OBSERVES_ONLY)
        self.assertEqual(found.removal, "forget")

    def test_a_managing_connection_offers_its_actions(self):
        connection("adguard", manages=True)

        found = resource_capabilities(self._rewrite())

        self.assertTrue(found.actions["reconcile"].enabled)
        self.assertEqual(found.removal, "delete")

    def test_no_connection_at_all_acts_like_observing(self):
        found = resource_capabilities(self._rewrite())

        self.assertFalse(found.actions["reconcile"].enabled)


class EveryWritePathFollowsTheConnectionTests(TestCase):
    """The capability shown on a page is the rule every write path enforces."""

    def setUp(self):
        from .security import cli_principal

        self.principal = cli_principal()
        self.resource = ManagedResource.objects.create(
            key="app-rewrite", kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            enabled=True, generation=2, observed_generation=1,
        )

    def test_queueing_through_an_observing_connection_is_refused(self):
        from .infrastructure import OperationCommand, PolicyError, request_reconcile

        connection("adguard", manages=False)

        with self.assertRaisesMessage(PolicyError, OBSERVES_ONLY):
            request_reconcile(
                OperationCommand(idempotency_key="k1"),
                principal=self.principal,
                current_key=self.resource.key,
            )

    def test_authoring_through_an_observing_connection_is_refused(self):
        from .infrastructure import ManagedResourceCommand, PolicyError, save_managed_resource

        connection("adguard", manages=False)

        with self.assertRaisesMessage(PolicyError, OBSERVES_ONLY):
            save_managed_resource(
                ManagedResourceCommand(
                    key="other-rewrite", kind="adguard.rewrite",
                    spec={"domain": "other.example.com", "answer": "10.0.0.11"},
                    enabled=True,
                ),
                principal=self.principal,
            )

    def test_the_scheduler_leaves_it_alone(self):
        from control_plane.models import OperationRequest

        from .controller import schedule_automatic_operations

        connection("adguard", manages=False)
        schedule_automatic_operations("example-controller")

        self.assertFalse(OperationRequest.objects.filter(resource=self.resource).exists())

    def test_a_managing_connection_queues(self):
        from .infrastructure import OperationCommand, request_reconcile

        connection("adguard", manages=True)

        result = request_reconcile(
            OperationCommand(idempotency_key="k2"),
            principal=self.principal,
            current_key=self.resource.key,
        )
        self.assertTrue(result["queued"])

    def test_the_scheduler_converges_through_a_managing_connection(self):
        from control_plane.models import OperationRequest

        from .controller import schedule_automatic_operations

        connection("adguard", manages=True)
        schedule_automatic_operations("example-controller")

        self.assertTrue(OperationRequest.objects.filter(resource=self.resource).exists())
