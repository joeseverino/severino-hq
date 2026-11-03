"""What an agent finds at /api/mcp/ is read from the server's own list answers."""

import asyncio
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from mcp import types
from mcp.server.lowlevel import Server

from hq.platform.application.tests.markup import elements, texts

from .. import listing
from ..server import mcp

RESOURCE = "https://hq.example.com/api"


@override_settings(SEVERINO_API_RESOURCE=RESOURCE)
class EndpointTests(SimpleTestCase):
    def test_it_names_the_endpoint_its_sign_in_and_every_tool_the_server_lists(self):
        found = listing.endpoint()

        self.assertEqual(found.url, f"{RESOURCE}/mcp/")
        self.assertEqual(found.sign_in, "https://hq.example.com/.well-known/oauth-protected-resource/api")
        self.assertTrue(found.protocol)
        tools = next(offer for offer in found.offers if offer.label == "Tools")
        self.assertEqual({item.name for item in tools.items}, set(mcp.tools))

    def test_a_list_the_server_comes_to_offer_appears_with_nothing_else_changed(self):
        async def prompts(_context, _params):
            return types.ListPromptsResult(prompts=[types.Prompt(name="example_prompt", description="An example.")])

        server = Server("Example", on_list_prompts=prompts)
        with mock.patch.object(listing, "mcp", mock.Mock(server=server)):
            found = listing.endpoint()

        self.assertEqual(
            [(offer.label, [item.name for item in offer.items]) for offer in found.offers],
            [("Prompts", ["example_prompt"])],
        )

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_without_an_api_resource_there_is_no_endpoint(self):
        self.assertIsNone(listing.endpoint())

    def test_it_answers_from_a_thread_already_running_an_event_loop(self):
        async def asked_inside_a_loop():
            return listing.endpoint()

        found = asyncio.run(asked_inside_a_loop())

        self.assertEqual(found.url, f"{RESOURCE}/mcp/")


class OfferedTests(SimpleTestCase):
    def test_a_description_s_first_paragraph_is_its_summary_and_the_rest_opens_under_it(self):
        offered = listing.Offered(
            "example_tool", "", "Reads an\nexample.\n\nNarrow it by `name`.\n\nA page is bounded."
        )

        self.assertEqual(offered.summary, "Reads an example.")
        self.assertEqual(offered.more, ("Narrow it by `name`.", "A page is bounded."))

    def test_a_description_of_one_paragraph_has_nothing_more(self):
        self.assertEqual(listing.Offered("example_tool", "", "Reads an example.").more, ())


@override_settings(SEVERINO_API_RESOURCE=RESOURCE)
class TitleTests(SimpleTestCase):
    def test_a_title_that_only_restates_the_name_is_left_out(self):
        async def prompts(_context, _params):
            return types.ListPromptsResult(
                prompts=[
                    types.Prompt(name="example-brief", title="Example brief", description="A brief."),
                    types.Prompt(name="example-needs", title="What needs me", description="The queue."),
                ]
            )

        server = Server("Example", on_list_prompts=prompts)
        with mock.patch.object(listing, "mcp", mock.Mock(server=server)):
            (offer,) = listing.endpoint().offers

        self.assertEqual(
            [(item.name, item.title) for item in offer.items],
            [("example-brief", ""), ("example-needs", "What needs me")],
        )

    def test_each_tool_shows_the_title_it_is_listed_under(self):
        tools = next(offer for offer in listing.endpoint().offers if offer.label == "Tools")

        self.assertEqual({item.title for item in tools.items}, {tool.title for tool in mcp.tools.values()})


@override_settings(SEVERINO_API_RESOURCE=RESOURCE)
class InstructionsTests(SimpleTestCase):
    def test_the_server_s_instructions_are_read_as_paragraphs(self):
        told = listing.endpoint().told

        self.assertGreater(len(told), 1)
        self.assertEqual(" ".join(told), " ".join((mcp.server.instructions or "").split()))


class CardTests(TestCase):
    """The Agents page's card draws a description's code spans as code, in the row and beneath it."""

    def test_a_tools_code_spans_are_code_and_its_markup_is_text(self):
        endpoint = listing.Endpoint(
            url=f"{RESOURCE}/mcp/",
            sign_in=RESOURCE,
            protocol="2026-07-28",
            instructions="Start with `hq://hq`.",
            offers=(
                listing.Offer(
                    "Tools",
                    (listing.Offered("example_tool", "", "Reads `example.read` <b>now</b>.\n\nThen `example_next`."),),
                ),
            ),
        )
        self.client.force_login(get_user_model().objects.create_user("operator"))

        with mock.patch("hq.platform.mcp.listing.endpoint", return_value=endpoint):
            part = self.client.get(reverse("agent_policy"), HTTP_X_FRAGMENT="mcp_endpoint")

        self.assertLessEqual({"hq://hq", "example.read", "example_next"}, set(texts(part, "code")))
        self.assertEqual(elements(part, "b"), [])
