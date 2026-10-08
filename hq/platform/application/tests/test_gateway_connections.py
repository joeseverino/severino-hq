"""Host gateways emit one connection/capability/resource contract."""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
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
    CLOUDFLARE_API_TOKEN="cloudflare-secret",
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
        # The account and database come from the observer's D1 reading.
        ProviderInventory.objects.create(
            kind="cloudflare.d1_database",
            records=[{"account_id": "a" * 32, "name": "contacts", "uuid": "database-id"}],
            observed_at=timezone.now(),
        )
        WeatherObservation.objects.create(
            point="41.8781,-87.6298",
            payload={"summary": "Chicago, IL", "metrics": []},
            observed_at=timezone.now(),
        )

    def test_host_gateways_emit_from_the_domain_registry(self):
        names = set(integration_graph().connections)

        self.assertLessEqual(
            {"hq.cloudflare_d1", "hq.public_registries", "hq.nws"},
            names,
        )
        # Retired: the GitHub App is HQ's one GitHub connection, and the public
        # read behind it holds no credential to show.
        self.assertNotIn("hq.github", names)

    def test_safe_runtime_catalog_contains_relationships_and_no_tokens(self):
        payload = list_connections(principal=OPERATOR)
        groups = {group["name"]: group for group in payload["groups"]}

        d1 = groups["hq.cloudflare_d1"]["instances"][0]
        self.assertEqual(
            {ability["capability"] for ability in d1["abilities"]},
            {
                "contact.submissions.list",
                "contact.submission.review",
                "contact.submission.delete",
            },
        )
        rendered = str(payload)
        self.assertNotIn("cloudflare-secret", rendered)
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

    @mock.patch("hq.platform.application.contact_submissions.d1.update_submission")
    @mock.patch("hq.platform.application.contact_submissions.d1.get_submission")
    def test_d1_review_runs_through_the_shared_executor(self, get, update):
        get.return_value = {"id": 7, "status": "unread"}

        result = execute_capability(
            "contact.submission.review",
            {"status": "read", "assigned_to": "joe", "admin_notes": "handled"},
            principal=OPERATOR,
            target=7,
        )

        self.assertTrue(result["ok"])
        update.assert_called_once_with(7, "read", "joe", "handled")

    @mock.patch("hq.platform.application.contact_submissions.d1.delete_submission")
    @mock.patch("hq.platform.application.contact_submissions.d1.get_submission", return_value=None)
    def test_d1_delete_retry_is_already_successful(self, get, delete):
        result = execute_capability(
            "contact.submission.delete",
            {"confirm": "7"},
            principal=OPERATOR,
            target=7,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["deleted"]["already_absent"])
        delete.assert_not_called()
