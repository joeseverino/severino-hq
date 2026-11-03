"""Adopting what the providers hold without changing it.

The safety property that makes one-click adoption defensible: the spec is read
back out of the live record, so the declaration starts equal to the world and
the first reconciliation is a no-op. Otherwise adopting a proxy host would
reset it to HQ's defaults (HSTS off, for one).
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.providers import PROVIDERS, validate_spec
from hq.platform.application.tests.writers import form_keyed
from hq.platform.core.models import AuditLog

from ..adoption import (
    AdoptCommand,
    AdoptServiceCommand,
    adopt,
    adopt_discovered,
    adopt_service,
    unmanaged,
    unmanaged_services,
)
from ..adoption_testing import managing_everything
from ..inventory import record_inventory
from ..record_errors import RecordNotFound
from ..report_testing import refused_part
from ..security import cli_principal
from ..sweep import record_sweep
from .inventory_records import A_PROXY, A_REWRITE, ANOTHER, a_sweep


class AdoptionTests(TestCase):
    def setUp(self):
        managing_everything()
        record_inventory(
            a_sweep(**{"npm.proxy_host": [A_PROXY], "adguard.rewrite": [A_REWRITE]}),
            principal=cli_principal(),
        )

    def test_adopting_captures_the_record_exactly_as_it_is(self):
        """The whole safety argument: the first reconcile after this is a no-op.

        Every setting comes from the live record. Adopting with defaults would
        turn HSTS off on a host that had it on.
        """
        adopt(
            AdoptCommand(kind="npm.proxy_host", hostname="shop.example.com"),
            principal=cli_principal(),
        )

        spec = ManagedResource.objects.get(kind="npm.proxy_host").spec
        self.assertTrue(spec["hsts_enabled"])
        self.assertTrue(spec["hsts_subdomains"])
        self.assertTrue(spec["trust_forwarded_proto"])
        self.assertTrue(spec["websocket"])
        self.assertEqual(spec["forward_port"], 3000)
        self.assertEqual(validate_spec("npm.proxy_host", spec), spec)

    def test_an_adopted_record_stops_being_offered(self):
        adopt(
            AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
            principal=cli_principal(),
        )

        self.assertNotIn("app.example.com", [item.hostname for item in unmanaged()])

    def test_adopting_twice_is_refused_rather_than_duplicated(self):
        adopt(
            AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
            principal=cli_principal(),
        )

        with self.assertRaises(RecordNotFound):
            adopt(
                AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
                principal=cli_principal(),
            )

    def test_adopting_something_no_longer_there_says_so(self):
        with self.assertRaises(RecordNotFound):
            adopt(
                AdoptCommand(kind="adguard.rewrite", hostname="ghost.example.com"),
                principal=cli_principal(),
            )

    def test_the_spec_is_read_from_the_record_and_named_after_its_hostname(self):
        """A browser could post a stale or edited copy; adoption captures truth."""
        ProviderInventory.objects.filter(kind="adguard.rewrite").update(
            records=[{"domain": "app.example.com", "answer": "10.0.0.55"}],
            observed_at=timezone.now(),
        )

        adopt(
            AdoptCommand(kind="adguard.rewrite", hostname="app.example.com"),
            principal=cli_principal(),
        )

        adopted = ManagedResource.objects.get(kind="adguard.rewrite")
        self.assertEqual(adopted.key, "app-example-com-dns")
        self.assertEqual(adopted.spec["answer"], "10.0.0.55")


class AdoptionWebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="test-only-password")
        self.client.force_login(self.user)
        managing_everything()
        # Stored without adopting, because the tests below exercise the manual
        # button, which only has anything to do when nothing took the record
        # first. A real sweep adopts, and the test for that says so itself.
        record_inventory(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())

    def test_a_swept_record_is_managed_without_being_opted_in(self):
        """Through a connection that manages, a swept record needs no click."""

        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())

        response = self.client.get(reverse("control_plane:services"))

        self.assertContains(response, "app.example.com")
        self.assertTrue(ManagedResource.objects.filter(kind="adguard.rewrite").exists())

    def test_adopting_from_the_page_creates_the_declaration(self):
        response = self.client.post(
            reverse("control_plane:adopt", kwargs={"hostname": "app.example.com"}), form_keyed()
        )

        resource = ManagedResource.objects.get(kind="adguard.rewrite")
        self.assertRedirects(
            response,
            reverse("control_plane:service", kwargs={"hostname": "app.example.com"}),
        )
        self.assertEqual(resource.spec["answer"], "10.0.0.10")

    def test_adopting_requires_a_signed_in_operator(self):
        self.client.logout()

        response = self.client.post(
            reverse("control_plane:adopt", kwargs={"hostname": "app.example.com"}), form_keyed()
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertFalse(ManagedResource.objects.exists())


class AdoptServiceTests(TestCase):
    """A hostname is one decision, even when it is several records."""

    def setUp(self):
        managing_everything()
        record_inventory(
            a_sweep(
                **{
                    "adguard.rewrite": [{"domain": "shop.example.com", "answer": "10.0.0.20"}],
                    "npm.proxy_host": [A_PROXY],
                }
            ),
            principal=cli_principal(),
        )

    def test_one_hostname_groups_its_records(self):
        services = unmanaged_services()

        self.assertEqual([s.hostname for s in services], ["shop.example.com"])
        self.assertEqual(len(services[0].items), 2)

    def test_the_grouped_columns_match_the_service_view_and_carry_values(self):
        """Both tables render the same columns, so a row lines up with a row.

        A cell carries the value and not its label: the heading already says DNS.
        """
        from hq.domains.control_plane.providers import service_facets

        facets = unmanaged_services()[0].facets

        self.assertEqual(
            [facet_id for facet_id, _, _ in facets],
            [facet_id for facet_id, _ in service_facets()],
        )
        # A facet nothing supplies for *this* service renders empty rather than
        # being dropped, so the columns still line up.
        values = {facet_id: value for facet_id, _, value in facets}
        self.assertEqual(values["certificate"], "")
        self.assertEqual(values["dns"], "10.0.0.20")
        self.assertEqual(values["proxy"], "http://10.0.0.20:3000")

    def test_adopting_a_hostname_takes_everything_behind_it(self):
        adopt_service(
            AdoptServiceCommand(hostname="shop.example.com"),
            principal=cli_principal(),
        )

        self.assertEqual(ManagedResource.objects.count(), 2)
        self.assertEqual(unmanaged_services(), ())

    def test_a_hostname_nobody_saw_is_refused(self):
        with self.assertRaises(RecordNotFound):
            adopt_service(
                AdoptServiceCommand(hostname="ghost.example.com"),
                principal=cli_principal(),
            )

    def test_a_failure_partway_adopts_nothing(self):
        """Half a service is worse than none: HQ would own the ingress and not
        the name, and the service page would show a gap that is not real."""
        ManagedResource.objects.create(key="shop-example-com-proxy", kind="adguard.rewrite", spec=A_REWRITE)

        with (
            mock.patch(
                "hq.platform.application.adoption.adopt",
                side_effect=[{"resource": {"key": "one"}}, RuntimeError("boom")],
            ),
            self.assertRaises(RuntimeError),
        ):
            adopt_service(
                AdoptServiceCommand(hostname="shop.example.com"),
                principal=cli_principal(),
            )

        self.assertEqual(ManagedResource.objects.count(), 1)


class NothingWaitsToBeOptedInTests(TestCase):
    """Through a connection that manages, everything adoptable is adopted.

    The one exception is a container no compose project declares: nobody
    decided it should exist, so it waits for a person and a finding says so.
    """

    def setUp(self):
        managing_everything(("cloudflare_dns", "a-token"), ("portainer", "a-portainer"))

    def swept(self, **kinds):
        record_sweep(a_sweep(**kinds), principal=cli_principal())
        return set(ManagedResource.objects.values_list("kind", flat=True))

    def test_a_rewrite_a_credential_reached_needs_no_click(self):
        self.assertIn("adguard.rewrite", self.swept(**{"adguard.rewrite": [A_REWRITE]}))

    def test_nothing_is_left_offering_itself_for_adoption(self):
        record_sweep(
            a_sweep(**{"adguard.rewrite": [A_REWRITE, ANOTHER]}),
            principal=cli_principal(),
        )

        self.assertEqual([item.hostname for item in unmanaged()], [])

    def test_everything_adoptable_is_adopted_and_only_a_stackless_container_waits(self):
        record_sweep(
            a_sweep(
                **{
                    "adguard.rewrite": [A_REWRITE, ANOTHER],
                    "npm.proxy_host": [A_PROXY],
                    "portainer.container": [A_COMPOSED_CONTAINER, A_STRAY_CONTAINER],
                }
            ),
            principal=cli_principal(),
        )

        waiting = unmanaged()

        self.assertEqual([item for item in waiting if item.adoptable], [])
        self.assertEqual(
            [(item.kind, item.identity) for item in waiting],
            [("portainer.container", ("a-docker-host", "a-stray"))],
        )

    def test_a_domain_the_credential_reaches_is_taken_on(self):

        kinds = self.swept(**{"cloudflare.zone": [{"zone": "example.com", "connection_ref": "a-token"}]})

        self.assertIn("cloudflare.zone", kinds)


class ASweepConfirmsWhatItFindsTests(TestCase):
    """A sweep is HQ going and looking, so it may write down what it saw.

    Nothing queues a reconcile for a resource that has not drifted, so if only
    a reconcile recorded an observation, an untouched declaration would read
    "never reported" indefinitely while every part of its service is running.
    Adoption is the one write that starts in sync, so it says so too.
    """

    def setUp(self):
        managing_everything()
        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())
        self.resource = ManagedResource.objects.get(kind="adguard.rewrite")

    def test_what_a_sweep_adopted_or_found_unchanged_is_observed_and_healthy(self):
        from hq.platform.application.infrastructure import resource_health

        for step in ("swept", "adopted again"):
            with self.subTest(step):
                if step == "adopted again":
                    adopt_discovered("adguard.rewrite", principal=cli_principal())
                    self.resource.refresh_from_db()
                self.assertEqual(self.resource.observed_generation, self.resource.generation)
                self.assertIsNotNone(self.resource.last_observed_at)
                self.assertEqual(self.resource.status.get("domain"), A_REWRITE["domain"])
                self.assertEqual(resource_health(self.resource)["state"], "healthy")

    def test_a_declaration_that_has_drifted_is_left_for_a_reconcile(self):
        """Calling drift "observed" hides the difference this model exists for."""

        self.resource.spec = {**self.resource.spec, "answer": "10.0.0.99"}
        self.resource.generation += 1
        self.resource.save(update_fields=["spec", "generation"])

        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())

        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.observed_generation, self.resource.generation)

    def test_a_provider_that_could_not_be_reached_confirms_nothing(self):
        before = ManagedResource.objects.get(kind="adguard.rewrite").last_observed_at

        record_sweep(
            {"adguard.rewrite": {"ok": False, "records": [], "error": "timed out"}},
            principal=cli_principal(),
        )

        self.assertEqual(ManagedResource.objects.get(kind="adguard.rewrite").last_observed_at, before)


A_COMPOSED_CONTAINER = {
    "name": "a-web",
    "host": "a-docker-host",
    "connection_ref": "a-portainer",
    "stack": "a-project",
}
A_STRAY_CONTAINER = {
    "name": "a-stray",
    "host": "a-docker-host",
    "connection_ref": "a-portainer",
    "stack": "",
}


class ASweepAdoptsOnlyWhatAComposeProjectDeclaresTests(TestCase):
    """A container is taken on unasked only when a compose file declares it.

    Everything that belongs on a machine is started by a compose project, so
    the label is the declaration. A container carrying none was started by hand
    or by something HQ does not know, and adopting it would make a container
    nobody declared look exactly like one somebody did.
    """

    def setUp(self):
        managing_everything(("portainer", "a-portainer"))
        record_sweep(
            a_sweep(**{"portainer.container": [A_COMPOSED_CONTAINER, A_STRAY_CONTAINER]}),
            principal=cli_principal(),
        )

    def names(self):
        return {resource.spec.get("name") for resource in ManagedResource.objects.filter(kind="portainer.container")}

    def test_only_a_container_its_compose_project_declares_is_adopted(self):
        names = self.names()

        self.assertIn("a-web", names)
        self.assertNotIn("a-stray", names)

    def test_it_stays_unmanaged_and_says_it_is_not_adoptable(self):
        (item,) = unmanaged()

        self.assertEqual(item.identity, ("a-docker-host", "a-stray"))
        self.assertFalse(item.adoptable)

    def test_a_later_sweep_still_leaves_it_waiting(self):
        """Not a first-pass accident: every sweep declines it the same way."""

        record_sweep(
            a_sweep(**{"portainer.container": [A_COMPOSED_CONTAINER, A_STRAY_CONTAINER]}),
            principal=cli_principal(),
        )

        self.assertNotIn("a-stray", self.names())

    def test_adopting_everything_of_its_kind_skips_it(self):
        self.assertEqual(
            adopt_discovered("portainer.container", principal=cli_principal()),
            {"adopted": []},
        )
        self.assertNotIn("a-stray", self.names())

    def test_a_person_can_still_adopt_it_by_its_token(self):
        """Declining to take it on unasked is not refusing to take it on."""

        (item,) = unmanaged()

        adopt(
            AdoptCommand(kind="portainer.container", token=item.token),
            principal=cli_principal(),
        )

        self.assertIn("a-stray", self.names())
        self.assertEqual(unmanaged(), ())


class ADeletedContainerIsForgottenTests(TestCase):
    """The listing includes stopped containers, so one missing from it is gone.

    HQ cannot create a container, so a declaration of a deleted one can never be
    met and would only raise a finding nobody can clear.
    """

    ELSEWHERE = {**A_COMPOSED_CONTAINER, "name": "b-web", "host": "b-docker-host"}

    def setUp(self):
        managing_everything(("portainer", "a-portainer"))
        record_sweep(
            a_sweep(**{"portainer.container": [A_COMPOSED_CONTAINER, self.ELSEWHERE]}),
            principal=cli_principal(),
        )

    def names(self):
        return {resource.spec.get("name") for resource in ManagedResource.objects.filter(kind="portainer.container")}

    def sweep(self, *records, **report):
        return record_sweep(
            {"portainer.container": {"ok": True, "records": list(records), **report}},
            principal=cli_principal(),
        )

    def test_one_its_machine_no_longer_lists_is_forgotten_until_it_returns(self):
        kept = {**A_COMPOSED_CONTAINER, "name": "a-db"}

        result = self.sweep(kept, self.ELSEWHERE)

        self.assertNotIn("a-web", self.names())
        self.assertEqual(len(result["retired"]), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.DELETED, message__contains="no longer on its machine"
            ).exists()
        )

        self.sweep(A_COMPOSED_CONTAINER, self.ELSEWHERE)

        self.assertIn("a-web", self.names())

    def test_a_machine_the_sweep_did_not_list_keeps_its_containers(self):
        """Unreachable lists nothing, which says nothing about what runs there."""

        self.sweep({**A_COMPOSED_CONTAINER, "name": "a-db"})

        self.assertIn("b-web", self.names())

    def test_a_refused_or_failed_listing_retires_nothing(self):
        self.sweep({**A_COMPOSED_CONTAINER, "name": "a-db"}, refused_parts=[refused_part("")])
        self.assertIn("a-web", self.names())
        record_sweep(
            {"portainer.container": {"ok": False, "records": [], "error": "down"}},
            principal=cli_principal(),
        )
        self.assertIn("a-web", self.names())

    def test_one_on_demand_is_kept(self):
        resource = ManagedResource.objects.get(spec__name="a-web")
        resource.spec = {**resource.spec, "on_demand": True}
        resource.save(update_fields=["spec"])

        self.sweep({**A_COMPOSED_CONTAINER, "name": "a-db"}, self.ELSEWHERE)

        self.assertIn("a-web", self.names())


class ADeclarationCanAlwaysBeGotRidOfTests(TestCase):
    """Removing a declaration must not depend on HQ being able to delete the
    thing it describes.

    A tailnet device joined by somebody running `tailscale up` on it. HQ never
    created it and has no delete for it, so removing the declaration forgets it.
    """

    def test_removal_is_offered_for_everything_hq_did_not_create(self):
        from hq.domains.control_plane.models import OperationRequest
        from hq.domains.control_plane.providers import controller_action_policy

        for kind, provider in sorted(PROVIDERS.items()):
            if provider.declaration_only:
                continue  # removal forgets the declaration; always available
            allowed, explanation = controller_action_policy(kind, OperationRequest.Action.DELETE)
            with self.subTest(kind=kind):
                self.assertTrue(
                    allowed or provider.removal_gap,
                    f"{kind} is not declaration-only, so removal queues a "
                    f"controller delete, which is refused: {explanation}. "
                    "Implement the delete, mark it declaration-only, or say in "
                    "`removal_gap` why its declarations cannot be removed.",
                )

    def test_a_stated_removal_gap_is_retired_once_removal_works(self):
        """Exemptions accrete, and each one hides a declaration nobody can
        get rid of."""

        from hq.domains.control_plane.models import OperationRequest
        from hq.domains.control_plane.providers import controller_action_policy

        for kind, provider in sorted(PROVIDERS.items()):
            if not provider.removal_gap:
                continue
            allowed, _ = controller_action_policy(kind, OperationRequest.Action.DELETE)
            with self.subTest(kind=kind):
                self.assertFalse(
                    allowed or provider.declaration_only,
                    f"{kind} can be removed now and should stop saying it cannot",
                )
