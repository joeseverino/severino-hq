"""The Agents page: each agent with its own block, the person's devices, and
what an agent finds at the MCP endpoint, asked of the server only when opened."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application import agent_registry, denials
from hq.platform.application.linked_accounts import SIGN_IN
from hq.platform.application.security import agent_principal
from hq.platform.application.tests.markup import select, text, within
from hq.platform.core.models import AgentIdentity, LinkedAccount, OwnDevice
from hq.platform.mcp.listing import Endpoint, Offer, Offered

AGENT = "example-agent"
PART = {"HTTP_X_FRAGMENT": "mcp_endpoint"}


def _seen(client_id: str) -> None:
    agent_registry.observe(agent_principal(client_id, "mcp", frozenset({"read"})))


def _submit(client, response, attribute: str, value: str):
    """Press the one button inside the element whose ``attribute`` is ``value``, as a browser would."""

    inside = within(response, attribute, value)
    (form,) = [element for element in inside if element.tag == "form"]
    fields = {
        element.attrs["name"]: element.attrs.get("value", "")
        for element in inside
        if element.tag in {"input", "button"}
        and element.attrs.get("name")
        and any(ancestor is form for ancestor in element.ancestors)
    }
    return client.post(form.attrs["action"], fields)


class AgentRowTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(self.person)
        _seen(AGENT)

    def page(self):
        return self.client.get(reverse("agent_policy"))

    def test_an_agents_block_blocks_it_and_then_allows_it(self):
        _submit(self.client, self.page(), "data-agent", AGENT)
        self.assertIsNotNone(AgentIdentity.objects.get(client_id=AGENT).blocked_at)
        self.assertTrue(select(self.page(), "data-blocked"))

        _submit(self.client, self.page(), "data-agent", AGENT)
        self.assertIsNone(AgentIdentity.objects.get(client_id=AGENT).blocked_at)

    def test_a_refusal_for_calling_too_much_is_on_its_row(self):
        denials.record_denial(interface="mcp", reason="rate_limited", actor=AGENT)

        row = " ".join(element.words for element in within(self.page(), "data-agent", AGENT))

        self.assertIn("refused", row)
        self.assertIn("calling too much", row)


class DeviceTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        LinkedAccount.objects.create(user=self.person, provider=SIGN_IN, login="example-key")
        self.client.force_login(self.person)

    def test_only_the_signed_in_persons_devices_are_shown_and_each_can_be_removed(self):
        other = get_user_model().objects.create_user("other", password="x" * 20)
        OwnDevice.objects.create(user=self.person, client_id="example-phone")
        OwnDevice.objects.create(user=other, client_id="example-tablet")

        page = self.client.get(reverse("agent_policy"))

        self.assertEqual([row.attrs["data-device"] for row in select(page, "data-device")], ["example-phone"])
        _submit(self.client, page, "data-device", "example-phone")
        self.assertEqual(list(OwnDevice.objects.values_list("client_id", flat=True)), ["example-tablet"])


ENDPOINT = Endpoint(
    url="https://hq.example.com/api/mcp/",
    sign_in="https://hq.example.com/.well-known/oauth-protected-resource/api",
    protocol="2026-07-28",
    instructions="",
    offers=(
        Offer("Tools", (Offered("example_tool", "Example tool", "Reads an example.", reads_only=True),)),
        Offer("Example later list", (Offered("example-entry", "", "Something the server added."),)),
    ),
)


class EndpointTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))

    def test_the_page_asks_nothing_of_the_mcp_server_until_it_is_opened(self):
        with mock.patch("hq.platform.mcp.listing.endpoint") as asked:
            self.client.get(reverse("agent_policy"))

        asked.assert_not_called()

    def test_opened_it_names_every_list_the_server_offers_and_nothing_else(self):
        with (
            mock.patch("hq.platform.mcp.listing.endpoint", return_value=ENDPOINT),
            mock.patch("hq.platform.application.agent_activity.standings") as standings,
        ):
            part = self.client.get(reverse("agent_policy"), **PART)

        said = text(part)
        for words in ("Tools", "example_tool", "Example later list", "example-entry", ENDPOINT.url, ENDPOINT.protocol):
            self.assertIn(words, said)
        self.assertNotIn("Your devices", said)
        standings.assert_not_called()

    def test_without_script_the_address_asks_for_it(self):
        with mock.patch("hq.platform.mcp.listing.endpoint", return_value=ENDPOINT):
            page = self.client.get(reverse("agent_policy"), {"endpoint": 1})

        self.assertIn("example_tool", text(page))
        self.assertIn("Your devices", text(page))

    def test_with_no_api_resource_it_says_agents_cannot_connect(self):
        with self.settings(SEVERINO_API_RESOURCE=""):
            part = self.client.get(reverse("agent_policy"), **PART)

        self.assertIn("cannot connect", text(part))
