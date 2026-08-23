"""Contract tests for HQ's shared read-resource registry."""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from projects.models import Project

from .command_center import command_center
from .resources import (
    EmptyQuery,
    InvalidResourceInput,
    ResourceSpec,
    describe_resources,
    get_resource,
    list_resource,
    resource_specs,
)
from .security import AuthorizationError, Capability, Principal


READ = Principal("reader", "test", frozenset({Capability.READ}))
NONE = Principal("nobody", "test", frozenset())
OPERATOR = Principal("operator", "test", frozenset(Capability))


class ResourceExecutionTests(TestCase):
    def test_one_spec_drives_discovery_list_and_detail(self):
        project = Project.objects.create(name="Resource registry")

        described = describe_resources()
        spec = next(
            item for item in described["resources"] if item["name"] == "projects"
        )
        listed = list_resource(
            "projects", {"query": "registry", "limit": 10}, principal=READ
        )

        self.assertEqual(spec["operations"]["get"]["identifier"], "slug")
        self.assertFalse(
            spec["operations"]["list"]["query_schema"]["additionalProperties"]
        )
        self.assertEqual(listed["items"][0]["slug"], project.slug)
        self.assertEqual(
            get_resource("projects", project.slug, principal=READ)["slug"],
            project.slug,
        )

    def test_command_center_derives_links_and_operations_from_the_registries(self):
        with self.assertNumQueries(0):
            outcome = command_center("certificate.renew", principal=OPERATOR)
        renewal = next(
            item for item in outcome["commands"] if item.name == "certificate.renew"
        )

        self.assertEqual(renewal.url, "/infrastructure/")
        self.assertEqual(renewal.badges, ("infrastructure change",))

    def test_command_center_degrades_an_unusable_plugin_route_to_text(self):
        plugin = ResourceSpec(
            "example.records",
            "Example records",
            "Synthetic plugin records.",
            Capability.READ,
            list_handler=lambda: {"items": [], "count": 0},
            list_query_type=EmptyQuery,
            web_route="missing:list",
        )
        with mock.patch(
            "application.resources.plugin_resource_specs", return_value=(plugin,)
        ):
            outcome = command_center("example.records", principal=OPERATOR)

        self.assertEqual(outcome["resources"][0].url, "")

    def test_command_center_omits_things_the_principal_cannot_use(self):
        outcome = command_center("", principal=NONE)

        self.assertEqual(
            outcome, {"resources": (), "commands": (), "connections": ()}
        )

    def test_unknown_filters_are_rejected_before_the_handler(self):
        with self.assertRaises(InvalidResourceInput):
            list_resource("projects", {"limti": 10}, principal=READ)

    def test_every_operation_authorizes_before_reading(self):
        with self.assertRaises(AuthorizationError):
            list_resource("projects", {}, principal=NONE)


class ResourceRegistrationTests(SimpleTestCase):
    def test_plugin_resource_names_cannot_shadow_core(self):
        duplicate = ResourceSpec(
            "projects",
            "Duplicate",
            "Must fail before an adapter silently wins by ordering.",
            Capability.READ,
            list_handler=lambda: {},
            list_query_type=EmptyQuery,
        )
        with (
            mock.patch(
                "application.resources.plugin_resource_specs",
                return_value=(duplicate,),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "Duplicate resource name"),
        ):
            resource_specs()

    def test_incomplete_list_contract_fails_at_registry_construction(self):
        invalid = ResourceSpec(
            "example.records",
            "Records",
            "A deliberately incomplete plugin contract.",
            "example.read",
            list_handler=lambda: {},
        )
        with (
            mock.patch(
                "application.resources.plugin_resource_specs", return_value=(invalid,)
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "handler and query together"),
        ):
            resource_specs()

    def test_nested_django_namespaces_are_valid_web_routes(self):
        nested = ResourceSpec(
            "example.records",
            "Records",
            "A nested plugin route.",
            Capability.READ,
            list_handler=lambda: {"items": [], "count": 0},
            list_query_type=EmptyQuery,
            web_route="example:records:list",
        )
        with mock.patch(
            "application.resources.plugin_resource_specs", return_value=(nested,)
        ):
            self.assertIn(nested, resource_specs())
