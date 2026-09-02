"""Contract tests for the one compiled integration graph."""

from types import MappingProxyType

from django.test import TestCase

from hq_sdk.capabilities import StrictCommand
from projects.models import Project

from .capabilities import CapabilitySpec, describe_capabilities
from .command_center import command_center
from .connections import ConnectionAbility, ConnectionSpec, describe_connections
from .integrations import (
    IntegrationGraphError,
    clear_integration_graph_cache,
    compile_integration_graph,
    integration_graph,
    override_integration_graph,
)
from .resources import EmptyQuery, ProjectQuery, ResourceSpec, describe_resources
from .search_contracts import SearchDefinition
from .security import Capability, Principal
from .plugins import clear_plugin_composition_cache


class SyntheticCommand(StrictCommand):
    pass


def execute_synthetic(command, *, principal, expected_updated_at):  # noqa: ARG001
    return {"ok": True}


class IntegrationGraphTests(TestCase):
    def tearDown(self):
        clear_integration_graph_cache()

    def test_compilation_is_query_free_and_immutable(self):
        with self.assertNumQueries(0):
            graph = integration_graph()

        self.assertIsInstance(graph.capabilities, MappingProxyType)
        self.assertIsInstance(graph.resources, MappingProxyType)
        self.assertIsInstance(graph.connections, MappingProxyType)
        self.assertIsInstance(graph.search, MappingProxyType)
        with self.assertRaises(TypeError):
            graph.connections["example.mutable"] = graph.connections["hq.github"]

    def test_the_composed_graph_is_compiled_once_until_explicitly_cleared(self):
        first = integration_graph()

        self.assertIs(integration_graph(), first)
        clear_integration_graph_cache()
        self.assertIsNot(integration_graph(), first)

    def test_clearing_plugin_composition_also_clears_the_derived_graph(self):
        first = integration_graph()

        clear_plugin_composition_cache()

        self.assertIsNot(integration_graph(), first)

    def test_compilation_reports_every_cross_spec_violation_together(self):
        resource = ResourceSpec(
            "example.records",
            "Records",
            "Synthetic records.",
            Capability.READ,
            list_handler=lambda **kwargs: {"items": [], "count": 0},
            list_query_type=ProjectQuery,
        )
        capabilities = (
            CapabilitySpec(
                "example.missing.read",
                "Read a missing resource.",
                "read",
                Capability.READ,
                SyntheticCommand,
                execute_synthetic,
                subject_resource="example.missing",
            ),
            CapabilitySpec(
                "example.records.update",
                "Update synthetic records.",
                "remote_write",
                Capability.READ,
                SyntheticCommand,
                execute_synthetic,
                subject_resource=resource.name,
                target_kind="slug",
                target_query=(("unknown", "value"),),
            ),
        )
        connection = ConnectionSpec(
            "example.gateway",
            "Gateway",
            "Synthetic gateway.",
            Capability.READ,
            lambda: (),
            abilities=(
                ConnectionAbility(
                    "missing.read",
                    "Read missing data",
                    "Read through a dangling edge.",
                    capability="example.unknown",
                    subject_resource="example.unknown",
                ),
            ),
        )

        with self.assertRaises(IntegrationGraphError) as raised:
            compile_integration_graph(
                capabilities=capabilities,
                resources=(resource,),
                connections=(connection,),
            )

        self.assertEqual(
            [violation.code for violation in raised.exception.violations],
            [
                "capability.unknown_resource",
                "capability.invalid_target_query",
                "connection.unknown_capability",
                "connection.unknown_resource",
            ],
        )
        self.assertIn("example.missing", str(raised.exception))
        self.assertIn("example.unknown", str(raised.exception))

    def test_standalone_search_is_part_of_the_compiled_graph(self):
        definition = SearchDefinition(
            "example.records", Project, "slug", ("name",)
        )

        graph = compile_integration_graph(
            capabilities=(), resources=(), connections=(), search=(definition,)
        )

        self.assertIs(graph.search[definition.scope], definition)

    def test_resource_and_standalone_search_scopes_cannot_collide(self):
        definition = SearchDefinition(
            "example.records", Project, "slug", ("name",)
        )
        resource = ResourceSpec(
            "example.records",
            "Records",
            "Synthetic records.",
            Capability.READ,
            search=definition,
        )

        with self.assertRaises(IntegrationGraphError) as raised:
            compile_integration_graph(
                capabilities=(),
                resources=(resource,),
                connections=(),
                search=(definition,),
            )

        self.assertEqual(
            [violation.code for violation in raised.exception.violations],
            ["duplicate.search"],
        )

    def test_invalid_search_contributions_name_their_origin_and_type(self):
        resource = ResourceSpec(
            "example.records",
            "Records",
            "Synthetic records.",
            Capability.READ,
            search="not a definition",
        )

        with self.assertRaises(IntegrationGraphError) as raised:
            compile_integration_graph(
                capabilities=(),
                resources=(resource,),
                connections=(),
                search=(object(),),
            )

        self.assertEqual(
            raised.exception.violations[0].subjects,
            (
                "resource 'example.records' returned str",
                "standalone contribution 0 returned object",
            ),
        )

    def test_a_connection_emits_its_capability_edge_once(self):
        graph = integration_graph()
        github = graph.connections["hq.github"]
        refresh = next(
            ability
            for ability in github.abilities
            if ability.name == "github.repository_metadata"
        )

        self.assertIs(graph.capabilities[refresh.capability], graph.capabilities["project.refresh"])

    def test_every_projection_sees_one_synthetic_contribution(self):
        resource = ResourceSpec(
            "example.records",
            "Synthetic records",
            "Synthetic extension records.",
            Capability.READ,
            list_handler=lambda: {"items": [], "count": 0},
            list_query_type=EmptyQuery,
        )
        capability = CapabilitySpec(
            "example.records.refresh",
            "Refresh synthetic extension records.",
            "read",
            Capability.READ,
            SyntheticCommand,
            execute_synthetic,
            subject_resource=resource.name,
        )
        connection = ConnectionSpec(
            "example.gateway",
            "Synthetic gateway",
            "Synthetic extension boundary.",
            Capability.READ,
            lambda: (),
            abilities=(
                ConnectionAbility(
                    "records.refresh",
                    "Refresh synthetic records",
                    "Read current synthetic records.",
                    capability=capability.name,
                    subject_resource=resource.name,
                ),
            ),
        )
        principal = Principal("operator", "test", frozenset({Capability.READ}))
        graph = compile_integration_graph(
            capabilities=(capability,),
            resources=(resource,),
            connections=(connection,),
        )

        with override_integration_graph(graph):
            discovery = command_center("synthetic", principal=principal)

            self.assertIn(capability.name, graph.capabilities)
            self.assertIn(resource.name, graph.resources)
            self.assertIn(connection.name, graph.connections)
            self.assertIn(
                capability.name,
                {item["name"] for item in describe_capabilities()["capabilities"]},
            )
            self.assertIn(
                resource.name,
                {item["name"] for item in describe_resources()["resources"]},
            )
            self.assertIn(
                connection.name,
                {item["name"] for item in describe_connections()["connections"]},
            )
            self.assertEqual(
                {
                    item.name
                    for group in ("resources", "commands", "connections")
                    for item in discovery[group]
                },
                {resource.name, capability.name, connection.name},
            )
