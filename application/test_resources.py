"""Contract tests for HQ's shared read-resource registry."""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from projects.models import Project

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
        self.assertEqual(get_resource("projects", project.slug, principal=READ)["slug"], project.slug)

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
