"""Host gateways emit one connection/capability/resource contract."""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from control_plane.models import DashboardConfiguration, WeatherObservation
from projects.models import Project

from .capabilities import capability_registry, execute_capability
from .command_center import command_center
from .connections import list_connections
from .integrations import integration_graph
from .security import Capability, Principal


OPERATOR = Principal(
    "operator",
    "test",
    frozenset(
        {
            Capability.READ,
            Capability.WRITE_PROJECTS,
            Capability.MANAGE_CONTACTS,
            Capability.LOOK_UP_PUBLIC_RECORDS,
        }
    ),
)


@override_settings(
    GITHUB_API_TOKEN="github-secret",
    CLOUDFLARE_ACCOUNT_ID="account-id",
    CLOUDFLARE_D1_DATABASE_ID="database-id",
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
        DashboardConfiguration.objects.create(
            weather_point="41.8781,-87.6298", weather_label="Chicago"
        )
        WeatherObservation.objects.create(
            point="41.8781,-87.6298",
            payload={"summary": "Chicago, IL", "metrics": []},
            observed_at=timezone.now(),
        )

    def test_host_gateways_emit_from_the_domain_registry(self):
        names = set(integration_graph().connections)

        self.assertLessEqual(
            {"hq.github", "hq.cloudflare_d1", "hq.public_registries", "hq.nws"},
            names,
        )

    def test_safe_runtime_catalog_contains_relationships_and_no_tokens(self):
        payload = list_connections(principal=OPERATOR)
        groups = {group["name"]: group for group in payload["groups"]}

        github = groups["hq.github"]["instances"][0]
        self.assertEqual(github["targets"][0]["label"], "Registered projects")
        self.assertEqual(
            github["abilities"][0]["capability"], "project.refresh"
        )
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
        self.assertNotIn("github-secret", rendered)
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

    def test_command_center_derives_github_process_from_the_connection(self):
        result = command_center("github", principal=OPERATOR)

        self.assertIn("hq.github", {item.name for item in result["connections"]})
        self.assertIn("project.refresh", {item.name for item in result["commands"]})

    @override_settings(GITHUB_API_TOKEN="")
    def test_github_still_emits_public_access_without_a_token(self):
        groups = {
            group["name"]: group
            for group in list_connections(principal=OPERATOR)["groups"]
        }

        github = groups["hq.github"]["instances"][0]
        self.assertEqual(github["status_label"], "public access")
        self.assertEqual(github["targets"][0]["label"], "Registered projects")

    def test_github_discovery_is_query_free(self):
        spec = integration_graph().connections["hq.github"]

        with self.assertNumQueries(0):
            instances = spec.instance_provider()

        self.assertEqual(instances[0].endpoint, "https://api.github.com")

    def test_capabilities_emit_the_process_steps_the_connection_names(self):
        registry = capability_registry()

        self.assertEqual(
            registry["project.refresh"].execution_notes[1],
            "Ask GitHub for current push metadata using the configured connection.",
        )
        self.assertEqual(
            registry["contact.submission.review"].subject_resource,
            "contact.submissions",
        )

    @mock.patch("application.contact_submissions.d1.update_submission")
    @mock.patch("application.contact_submissions.d1.get_submission")
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

    @mock.patch("application.contact_submissions.d1.delete_submission")
    @mock.patch("application.contact_submissions.d1.get_submission", return_value=None)
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
