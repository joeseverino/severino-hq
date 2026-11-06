"""Each MCP list names a caller only what it can use: a tool, prompt, fixed
resource or resource template is listed exactly when using it is not refused
for want of a grant.

A tool that runs or reads by kind is usable when its catalog says some entry is
permitted; everything else, when using it is not answered ``forbidden``.
"""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client
from mcp.shared.exceptions import MCPError

from hq.platform.application.security import DEVICE_INTERFACE, Capability, Principal, operator_capabilities

from .. import prompts, resources
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
# A record no kind holds: reading it is refused for the grant before it is looked for.
MISSING = "example-missing"


async def _not_forbidden(use):
    """Whether ``use()`` is answered anything but a refusal for want of a grant."""

    try:
        await use()
    except MCPError as error:
        return (error.error.data or {}).get("code") != "forbidden"
    return True


async def _tools(client, server):
    listed = {tool.name for tool in (await client.list_tools()).tools}
    usable = {}
    for name in server.tools:
        if name in BY_CATALOG:
            catalog, field, serves = BY_CATALOG[name]
            entries = (await client.call_tool(catalog, {})).structured_content[field]
            usable[name] = any(entry["permitted"] and serves(entry) for entry in entries)
        else:
            answer = await client.call_tool(name, ARGUMENTS.get(name, {}))
            usable[name] = not answer.is_error or answer.structured_content["error"]["code"] != "forbidden"
    return listed, usable


async def _prompts(client, _server):
    listed = {prompt.name for prompt in (await client.list_prompts()).prompts}
    usable = {name: await _not_forbidden(lambda name=name: client.get_prompt(name)) for name in prompts.PROMPTS}
    return listed, usable


async def _views(client, _server):
    listed = {str(view.uri) for view in (await client.list_resources()).resources}
    uris = [view.uri for view in resources.VIEWS.values()]
    usable = {uri: await _not_forbidden(lambda uri=uri: client.read_resource(uri)) for uri in uris}
    return listed, usable


async def _templates(client, _server):
    listed = {item.name for item in (await client.list_resource_templates()).resource_templates}
    usable = {}
    for kind, (template, spec) in resources._records().items():
        uri = template.expand({spec.identifier: MISSING})
        usable[kind] = await _not_forbidden(lambda uri=uri: client.read_resource(uri))
    return listed, usable


LISTS = {"tools": _tools, "prompts": _prompts, "resources": _views, "templates": _templates}


def _asked(principal):
    """For each list, what it names to ``principal``, and whether each declared entry is usable by it."""

    async def run():
        async with Client(mcp.server) as client:
            return {name: await asked(client, mcp) for name, asked in LISTS.items()}

    with set_principal(principal):
        return async_to_sync(run)()


class ListedByGrantTests(TestCase):
    def test_each_entry_is_listed_exactly_when_its_caller_can_use_it(self):
        sizes = {kind: set() for kind in LISTS}
        for principal in _callers():
            for kind, (listed, usable) in _asked(principal).items():
                sizes[kind].add(len(listed))
                for name, can in usable.items():
                    with self.subTest(caller=principal.actor, list=kind, entry=name):
                        self.assertEqual(name in listed, can)
        # Each list is the caller's: one caller is offered more of it than another.
        for kind, seen in sizes.items():
            with self.subTest(list=kind):
                self.assertGreater(len(seen), 1)

    def test_the_catalogs_are_listed_to_every_caller(self):
        listed, _ = _asked(_callers()[0])["tools"]

        self.assertLessEqual({"describe_capabilities", "describe_resources", "describe_connections"}, listed)
