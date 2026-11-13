"""What HQ offers to do with a resource follows whether its connection manages."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource

from ..adoption_testing import connection
from ..resource_capabilities import resource_capabilities


def _gap(kind: str):
    """What a change of ``kind`` lacks, as every write path and page says it."""

    from ..adoption import write_gap

    return write_gap(kind, {})


class CapabilitiesFollowTheConnectionTests(TestCase):
    def _rewrite(self):
        return ManagedResource.objects.create(
            key="app-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            enabled=True,
        )

    def test_an_observing_connection_offers_nothing_it_cannot_do(self):
        connection("adguard", manages=False)

        found = resource_capabilities(self._rewrite())

        self.assertFalse(found.actions["reconcile"].enabled)
        self.assertEqual(found.actions["reconcile"].reason, _gap("adguard.rewrite").reason)
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
        from ..security import cli_principal

        self.principal = cli_principal()
        self.resource = ManagedResource.objects.create(
            key="app-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            enabled=True,
            generation=2,
            observed_generation=1,
        )

    def test_queueing_through_an_observing_connection_is_refused(self):
        from ..record_errors import PolicyError
        from ..resource_operations import OperationCommand, request_reconcile

        connection("adguard", manages=False)

        with self.assertRaisesMessage(PolicyError, _gap("adguard.rewrite").refusal):
            request_reconcile(
                OperationCommand(idempotency_key="k1"),
                principal=self.principal,
                current_key=self.resource.key,
            )

    def test_authoring_through_an_observing_connection_is_refused(self):
        from ..infrastructure import ManagedResourceCommand, save_managed_resource
        from ..record_errors import PolicyError

        connection("adguard", manages=False)

        with self.assertRaisesMessage(PolicyError, _gap("adguard.rewrite").refusal):
            save_managed_resource(
                ManagedResourceCommand(
                    key="other-rewrite",
                    kind="adguard.rewrite",
                    spec={"domain": "other.example.com", "answer": "10.0.0.11"},
                    enabled=True,
                ),
                principal=self.principal,
            )

    def test_the_scheduler_leaves_it_alone(self):
        from hq.domains.control_plane.models import OperationRequest

        from ..controller import schedule_automatic_operations

        connection("adguard", manages=False)
        schedule_automatic_operations("example-controller")

        self.assertFalse(OperationRequest.objects.filter(resource=self.resource).exists())

    def test_a_managing_connection_queues(self):
        from ..resource_operations import OperationCommand, request_reconcile

        connection("adguard", manages=True)

        result = request_reconcile(
            OperationCommand(idempotency_key="k2"),
            principal=self.principal,
            current_key=self.resource.key,
        )
        self.assertTrue(result["queued"])

    def test_the_scheduler_converges_through_a_managing_connection(self):
        from hq.domains.control_plane.models import OperationRequest

        from ..controller import schedule_automatic_operations

        connection("adguard", manages=True)
        schedule_automatic_operations("example-controller")

        self.assertTrue(OperationRequest.objects.filter(resource=self.resource).exists())


class EveryKindsChangesFollowItsConnectionTests(TestCase):
    """Every kind HQ changes through a connection, each verb its controller
    applies: with no connection of its providers that manages, none is offered,
    each saying what the change needs, and the page links where that is set up."""

    def test_no_change_is_offered_through_a_connection_that_only_reads(self):
        from hq.domains.control_plane.providers import PROVIDERS

        from ..resource_capabilities import LIFECYCLE_VERBS

        kinds = sorted(
            kind for kind, spec in PROVIDERS.items() if spec.connection_providers and not spec.declaration_only
        )
        self.assertTrue(kinds)
        for kind in kinds:
            for provider in PROVIDERS[kind].connection_providers:
                connection(provider, manages=False)
        for kind in kinds:
            with self.subTest(kind=kind):
                resource = ManagedResource(key="example-record", kind=kind, spec={}, enabled=True)
                found = resource_capabilities(resource, running=tuple(LIFECYCLE_VERBS), removal_pending=False)
                gap = found.write_gap

                self.assertIsNotNone(gap)
                self.assertIn("manages: 1", gap.needs)
                self.assertIn("?setup=", gap.setup_url)
                for verb, allowed in found.actions.items():
                    self.assertFalse(allowed.enabled, verb)
                    self.assertEqual(allowed.reason, gap.reason, verb)
                self.assertNotEqual(found.removal, "delete")

    def test_a_managing_connection_leaves_nothing_to_set_up(self):
        from hq.domains.control_plane.providers import PROVIDERS

        kinds = sorted(
            kind for kind, spec in PROVIDERS.items() if spec.connection_providers and not spec.declaration_only
        )
        for kind in kinds:
            for provider in PROVIDERS[kind].connection_providers:
                connection(provider, manages=True)
        for kind in kinds:
            with self.subTest(kind=kind):
                resource = ManagedResource(key="example-record", kind=kind, spec={}, enabled=True)

                self.assertIsNone(resource_capabilities(resource, running=(), removal_pending=False).write_gap)

    def test_a_records_page_links_the_step_that_lets_it_change(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        connection("adguard", manages=False)
        resource = ManagedResource.objects.create(
            key="app-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.10"},
            enabled=True,
        )
        self.client.force_login(get_user_model().objects.create_superuser("op"))

        page = self.client.get(resource.get_absolute_url()).content.decode()

        self.assertIn(f'href="{reverse("control_plane:connections")}?setup=adguard#setup-adguard"', page)
        self.assertIn("manages: 1", page)


class WhatAChangeLacksTests(TestCase):
    """What a refused change says is missing comes from what HQ knows of the
    connection: its flag, the connection itself, and the permissions a change
    needs, which a connection never reports."""

    def test_a_connection_whose_item_does_not_manage_is_named_with_its_flag(self):
        connection("cloudflare_api", "example-cloudflare", manages=False)

        gap = _gap("cloudflare.contact_inbox")

        self.assertIn("example-cloudflare", gap.reason)
        self.assertIn("manages", gap.reason)
        self.assertNotIn("No Cloudflare API connection", gap.reason)

    def test_no_connection_says_one_is_needed(self):
        gap = _gap("cloudflare.contact_inbox")

        self.assertIn("No Cloudflare API connection", gap.reason)
        self.assertNotIn("manages: 1", gap.needs)

    def test_the_permissions_a_change_needs_are_said_as_unchecked(self):
        from ..connection_catalog import EVIDENCE_DETAILS

        connection("cloudflare_api", "example-cloudflare", manages=False)

        gap = _gap("cloudflare.contact_inbox")

        self.assertIn("D1 Write (account)", gap.needs)
        self.assertIn(EVIDENCE_DETAILS["unknown"], gap.reason)

    def test_a_connection_whose_changes_need_no_permission_names_only_its_flag(self):
        connection("adguard", manages=False)

        self.assertEqual(_gap("adguard.rewrite").needs, "Needs manages: 1")
