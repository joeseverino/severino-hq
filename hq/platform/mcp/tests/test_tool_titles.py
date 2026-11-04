"""A client shows a person each tool by its title, so the listing gives every tool one written for a person."""

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp import Client

from ..identity import set_principal
from ..listing import widest_caller
from ..server import mcp


def _listed():
    async def listed():
        with set_principal(widest_caller()):
            async with Client(mcp.server) as client:
                return await client.list_tools()

    return async_to_sync(listed)().tools


class ToolTitleTests(SimpleTestCase):
    def test_every_tool_is_listed_under_a_title_of_its_own_that_is_not_its_name_respelled(self):
        tools = _listed()

        titles = [tool.title for tool in tools]
        self.assertEqual(len(set(titles)), len(tools))
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.title)
                self.assertNotEqual(tool.title.lower(), tool.name.replace("_", " "))
                # Shown before annotations.title, so one title is the whole of it.
                self.assertIsNone(tool.annotations.title)
