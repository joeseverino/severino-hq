"""Host gateways emit one connection/capability/resource contract."""

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
    WeatherObservation,
)
from hq.domains.projects.models import Project

from ..capabilities import capability_registry, execute_capability
from ..connections import list_connections
from ..integrations import integration_graph
from ..security import Capability, Principal

OPERATOR = Principal(
    "operator",
    "test",
    frozenset(
        {
            Capability.READ,
            "write_projects",
            Capability.MANAGE_CONTACTS,
            Capability.LOOK_UP_PUBLIC_RECORDS,
        }
    ),
)


@override_settings(
    SEVERINO_LOOKUP_ENDPOINT="https://resolver.example",
    SEVERINO_RDAP_ENDPOINT="https://rdap.example",
)
class GatewayConnectionTests(TestCase):
    def setUp(self):
        Project.objects.create(
            name="Example",
            slug="example",
            repository_url="https://github.com/example/project",
        )
        DashboardConfiguration.objects.create(weather_point="41.8781,-87.6298", weather_label="Chicago")
        # The messages the controller read through the cloudflare_api connection.
        ProviderInventory.objects.create(
            kind="cloudflare.contact_submission",
            records=[
                {
                    "connection_ref": "example-cloudflare",
                    "account_id": "a" * 32,
                    "database_id": "database-id",
                    "database": "contacts",
                    "id": 7,
                    "status": "unread",
                }
            ],
            observed_at=timezone.now(),
        )
        ProviderConnection.objects.create(
            connection_ref="example-cloudflare", provider="cloudflare_api", manages=True, observed_at=timezone.now()
        )
        WeatherObservation.objects.create(
            point="41.8781,-87.6298",
            payload={"summary": "Chicago, IL", "metrics": []},
            observed_at=timezone.now(),
        )

    def test_host_gateways_emit_from_the_domain_registry(self):
        names = set(integration_graph().connections)

        self.assertLessEqual({"hq.public_registries", "hq.nws"}, names)
        # The contact inbox is read and written by the controller through the
        # Cloudflare API connection, so it has no connection of its own here.
        self.assertNotIn("hq.cloudflare_d1", names)
        # Retired: the GitHub App is HQ's one GitHub connection, and the public
        # read behind it holds no credential to show.
        self.assertNotIn("hq.github", names)

    def test_safe_runtime_catalog_contains_relationships_and_no_tokens(self):
        payload = list_connections(principal=OPERATOR)
        groups = {group["name"]: group for group in payload["groups"]}

        nws = groups["hq.nws"]["instances"][0]
        self.assertEqual(nws["status_label"], "keyless")
        self.assertEqual(nws["targets"][0]["label"], "Dashboard weather")
        # No command performs a weather read, so the abilities name none. They
        # named the privileged controller refresh, which resolved and so passed
        # every check, and rendered a button to wake the controller under the
        # label "Active weather alerts".
        self.assertEqual(
            {ability["capability"] for ability in nws["abilities"]},
            {None},
        )

    def test_nws_discovery_is_query_free(self):
        spec = integration_graph().connections["hq.nws"]

        with self.assertNumQueries(0):
            instances = spec.instance_provider()

        self.assertEqual(instances[0].endpoint, "https://api.weather.gov")

    def test_capabilities_emit_the_process_steps_the_connection_names(self):
        registry = capability_registry()

        self.assertIn(
            "public API, which needs no credential",
            " ".join(registry["project.refresh"].execution_notes),
        )
        self.assertEqual(
            registry["contact.submission.review"].subject_resource,
            "contact.submissions",
        )

    def test_a_review_runs_through_the_shared_executor_and_is_queued(self):
        result = execute_capability(
            "contact.submission.review",
            {"status": "read", "assigned_to": "joe", "admin_notes": "handled"},
            principal=OPERATOR,
            target=7,
        )

        self.assertTrue(result["ok"])
        spec = ManagedResource.objects.get(kind="cloudflare.contact_inbox").spec
        self.assertEqual(spec["reviews"], [{"id": 7, "status": "read", "assigned_to": "joe", "admin_notes": "handled"}])

    def test_a_delete_retry_is_already_successful(self):
        ProviderInventory.objects.filter(kind="cloudflare.contact_submission").update(records=[])
        result = execute_capability(
            "contact.submission.delete",
            {"confirm": "7"},
            principal=OPERATOR,
            target=7,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["deleted"]["already_absent"])
        self.assertFalse(ManagedResource.objects.exists())
