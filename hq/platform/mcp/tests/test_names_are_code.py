"""Every name an agent calls or reads by is written as code where the server speaks of it.

A client that renders Markdown, and the Agents page through ``code_spans``,
draws only a backticked span as code, so a bare name reads as a word.
"""

import re

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from mcp import Client

from hq.platform.application.refusal_status import CODES

from ..identity import set_principal
from ..listing import widest_caller
from ..server import mcp

CODE_SPAN = re.compile(r"`[^`]*`")


def _listed():
    """Each name the server lists (tools, prompts, resources, resource templates) and each description."""

    async def listed():
        async with Client(mcp.server) as client:
            tools = (await client.list_tools()).tools
            prompts = (await client.list_prompts()).prompts
            resources = (await client.list_resources()).resources
            templates = (await client.list_resource_templates()).resource_templates
        # A template's name is its kind, a plain word in prose; its address is the name an agent reads by.
        names = {
            *(tool.name for tool in tools),
            *(prompt.name for prompt in prompts),
            *(str(resource.uri) for resource in resources),
            *(template.uri_template for template in templates),
        }
        described = {
            item_name: item.description or ""
            for item_name, item in (
                *((tool.name, tool) for tool in tools),
                *((prompt.name, prompt) for prompt in prompts),
                *((str(resource.uri), resource) for resource in resources),
                *((template.uri_template, template) for template in templates),
            )
        }
        return names, described

    with set_principal(widest_caller()):
        return async_to_sync(listed)()


def _bare(text, names):
    """The names ``text`` holds outside its code spans."""

    prose = CODE_SPAN.sub(" ", text)
    return sorted(name for name in names if re.search(rf"(?<![\w./:-]){re.escape(name)}(?![\w/-])", prose))


class NamesAreCodeTests(SimpleTestCase):
    def test_the_instructions_write_every_listed_name_and_refusal_code_as_code(self):
        names, _ = _listed()
        # A code of one word reads as that word in prose, so only a compound one is held here.
        codes = {code for code in CODES if "_" in code}

        self.assertEqual(_bare(mcp.server.instructions or "", names | codes), [])

    def test_each_description_listed_writes_every_listed_name_as_code(self):
        names, described = _listed()

        for name, description in described.items():
            with self.subTest(listed=name):
                self.assertEqual(_bare(description, names), [])
