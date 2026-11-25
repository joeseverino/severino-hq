"""A model picks between neighboring reads by what each description says of the others.

Each pointer below is held to a name the server lists (a tool, a prompt or a
resource), so a renamed or retired neighbor fails here rather than leaving a
description that sends a model to nothing.
"""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from ..identity import set_principal
from ..listing import widest_caller
from ..server import mcp

# Where a description is read, and the neighbors it names as the better call for something.
POINTERS = {
    "system_health": {"get_findings", "dashboard_snapshot", "status-brief"},
    "dashboard_snapshot": {"system_health", "get_findings", "status-brief"},
    "get_findings": {"system_health", "status-brief"},
    "hq://hq": {"system_health", "status-brief"},
    "audit_registry": {"list_resource"},
    "export_year_summary": {"dashboard_snapshot"},
}


def _listing(principal):
    """Each name listed to ``principal`` and its description: tools, prompts and fixed resources."""

    async def listed():
        async with Client(mcp.server) as client:
            tools = (await client.list_tools()).tools
            prompts = (await client.list_prompts()).prompts
            resources = (await client.list_resources()).resources
        return {
            **{tool.name: tool.description for tool in tools},
            **{prompt.name: prompt.description for prompt in prompts},
            **{str(resource.uri): resource.description for resource in resources},
        }

    with set_principal(principal):
        return async_to_sync(listed)()


class PointerTests(TestCase):
    def test_each_pointer_names_something_the_server_lists_and_its_description_says_it(self):
        listing = _listing(widest_caller())

        for source, targets in POINTERS.items():
            for target in targets:
                with self.subTest(source=source, target=target):
                    self.assertIn(target, listing)
                    self.assertIn(target, listing[source])
