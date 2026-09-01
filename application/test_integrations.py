"""Contract tests for the one compiled integration graph."""

from types import MappingProxyType
from unittest import mock

from django.test import TestCase

from hq_sdk.capabilities import StrictCommand

from .capabilities import CapabilitySpec, describe_capabilities
from .command_center import command_center
from .connections import ConnectionAbility, ConnectionSpec, describe_connections
from .integrations import integration_graph
from .plugins import (
    plugin_capability_specs,
    plugin_connection_specs,
    plugin_resource_specs,
)
from .resources import EmptyQuery, ResourceSpec, describe_resources
from .security import Capability, Principal


class SyntheticCommand(StrictCommand):
    pass


def execute_synthetic(command, *, principal, expected_updated_at):  # noqa: ARG001
    return {"ok": True}


class IntegrationGraphTests(TestCase):
    def test_compilation_is_query_free_and_immutable(self):
        with self.assertNumQueries(0):
            graph = integration_graph()

        self.assertIsInstance(graph.capabilities, MappingProxyType)
        self.assertIsInstance(graph.resources, MappingProxyType)
        self.assertIsInstance(graph.connections, MappingProxyType)
        with self.assertRaises(TypeError):
            graph.connections["example.mutable"] = graph.connections["hq.github"]

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
        capabilities = (*plugin_capability_specs(), capability)
        resources = (*plugin_resource_specs(), resource)
        connections = (*plugin_connection_specs(), connection)
        principal = Principal("operator", "test", frozenset({Capability.READ}))

        with (
            mock.patch(
                "application.capabilities.plugin_capability_specs",
                return_value=capabilities,
            ),
            mock.patch(
                "application.resources.plugin_resource_specs",
                return_value=resources,
            ),
            mock.patch(
                "application.plugins.plugin_connection_specs",
                return_value=connections,
            ),
        ):
            graph = integration_graph()
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
