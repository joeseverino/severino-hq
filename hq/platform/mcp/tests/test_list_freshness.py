"""A list says how long it stays fresh: what changes only with a release is kept, what is the caller's is asked again.

Read as a client of the current protocol reads it, so the hint is the one on the wire.
"""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.platform.application.security import mcp_principal

from ..identity import set_principal
from ..server import mcp


class ListFreshnessTests(TestCase):
    def test_the_release_bound_lists_are_kept_privately_and_the_callers_templates_are_not(self):
        async def listed():
            async with Client(mcp.server) as client:
                return (
                    await client.list_tools(),
                    await client.list_prompts(),
                    await client.list_resources(),
                    await client.list_resource_templates(),
                )

        with set_principal(mcp_principal()):
            tools, prompts, views, templates = async_to_sync(listed)()

        for name, answer in (("tools", tools), ("prompts", prompts), ("resources", views)):
            with self.subTest(name):
                self.assertGreater(answer.ttl_ms, 0)
                self.assertEqual(answer.cache_scope, "private")
        self.assertEqual((templates.ttl_ms, templates.cache_scope), (0, "private"))
