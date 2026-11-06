"""Each projection's ``schema_version`` is one fact: the number its answer
carries is the one the API document and its MCP tool declare."""

from django.test import TestCase

from hq.platform.application.capabilities import describe_capabilities
from hq.platform.application.connections import describe_connections
from hq.platform.application.findings import findings
from hq.platform.application.resources import describe_resources
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.host_only import host_only
from hq.platform.application.topology import topology
from hq.platform.mcp.declarations import tool_contract
from hq.platform.mcp.finding_actions import agent_findings

from .. import openapi


class ShapeVersionTests(TestCase):
    def test_every_door_declares_the_version_its_projection_answers(self):
        principal = cli_principal()
        with host_only():
            answers = {
                "describe_capabilities": ("CapabilityRequestCatalog", describe_capabilities(principal=principal)),
                "describe_resources": ("ResourceCatalog", describe_resources(principal=principal)),
                "describe_connections": ("ConnectionCatalog", describe_connections(principal=principal)),
                "get_findings": (None, agent_findings(principal=principal)),
                "get_topology": (None, topology(principal=principal)),
            }
            # MCP writes findings in a shape of its own, versioned apart from the API's.
            shared = findings(principal=principal)
            schemas = openapi.document()["components"]["schemas"]
            tools = tool_contract()

        self.assertEqual(schemas["Findings"]["properties"]["schema_version"].get("const"), shared["schema_version"])

        for tool, (catalog, answer) in answers.items():
            with self.subTest(tool=tool):
                version = answer["schema_version"]
                success = tools[tool]["outputSchema"]["anyOf"][0]
                self.assertEqual(success["properties"]["schema_version"].get("const"), version)
                if catalog is not None:
                    self.assertEqual(schemas[catalog]["properties"]["schema_version"].get("const"), version)
