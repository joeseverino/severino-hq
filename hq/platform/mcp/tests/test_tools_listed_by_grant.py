"""tools/list names a caller only the tools it can use: a tool is listed exactly
when calling it is not refused for want of a grant.

A tool that runs or reads by kind is usable when its catalog says some entry is
permitted; every other tool, when a call is not answered ``forbidden``.
"""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.platform.application.security import DEVICE_INTERFACE, Capability, Principal, operator_capabilities

from ..identity import set_principal
from ..server import mcp


def _callers():
    return (
        Principal("example-none", "mcp", frozenset()),
        Principal("example-reader", "mcp", frozenset({Capability.READ})),
        Principal("example-auditor", "mcp", frozenset({Capability.READ, Capability.READ_AUDIT_LOG})),
        Principal("example-writer", "mcp", frozenset({Capability.READ, "write_projects"})),
        Principal("example-device", DEVICE_INTERFACE, operator_capabilities()),
    )


ARGUMENTS = {"export_year_summary": {"year": 2026}}
# The tools run or read by kind, and the catalog entry each is usable by.
BY_CATALOG = {
    "execute_capability": ("describe_capabilities", "capabilities", lambda entry: True),
    "list_resource": ("describe_resources", "resources", lambda entry: entry["operations"]["list"] is not None),
    "get_resource": ("describe_resources", "resources", lambda entry: entry["operations"]["get"] is not None),
}


def _asked(principal):
    """The tools listed to ``principal``, and whether each declared tool is usable by it."""

    async def run():
        async with Client(mcp.server) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            usable = {}
            for name in mcp.tools:
                if name in BY_CATALOG:
                    catalog, field, serves = BY_CATALOG[name]
                    entries = (await client.call_tool(catalog, {})).structured_content[field]
                    usable[name] = any(entry["permitted"] and serves(entry) for entry in entries)
                else:
                    answer = await client.call_tool(name, ARGUMENTS.get(name, {}))
                    usable[name] = not answer.is_error or answer.structured_content["error"]["code"] != "forbidden"
            return listed, usable

    with set_principal(principal):
        return async_to_sync(run)()


class ToolsListedByGrantTests(TestCase):
    def test_a_tool_is_listed_exactly_when_its_caller_can_use_it(self):
        for principal in _callers():
            listed, usable = _asked(principal)
            for name, can in usable.items():
                with self.subTest(caller=principal.actor, tool=name):
                    self.assertEqual(name in listed, can)

    def test_the_catalogs_are_listed_to_every_caller(self):
        listed, _ = _asked(_callers()[0])

        self.assertLessEqual({"describe_capabilities", "describe_resources", "describe_connections"}, listed)
