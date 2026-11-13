"""The Agents page: each agent with its own block and the tailnet devices it
calls from, the person's devices, and what an agent finds at the MCP endpoint,
asked of the server only when opened."""

import re
from dataclasses import replace
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from hq.platform.application import agent_access, agent_registry, denials, roles
from hq.platform.application.linked_accounts import SIGN_IN
from hq.platform.application.security import Capability, Principal, agent_grant, agent_grantable, agent_principal
from hq.platform.application.tests.markup import elements, select, text, within
from hq.platform.application.tokens import DEVICE_SCOPE
from hq.platform.core.audit import operation_context
from hq.platform.core.models import AgentDevice, AgentIdentity, AuditLog, LinkedAccount, OwnDevice
from hq.platform.core.test_runner import no_default_role
from hq.platform.mcp.listing import Endpoint, Offer, Offered

AGENT = "example-agent"
PART = {"HTTP_X_FRAGMENT": "mcp_endpoint"}


def _seen(client_id: str) -> None:
    agent_registry.observe(agent_principal(client_id, "mcp", frozenset({"read"})))


def _submit(client, response, attribute: str, value: str, pressing: str = ""):
    """Press the one button inside the element whose ``attribute`` is ``value``, as a browser would;
    with ``pressing``, the one of several that sends that field."""

    inside = within(response, attribute, value)
    forms = [element for element in inside if element.tag == "form"]
    if pressing:
        forms = [
            form
            for form in forms
            if any(
                element.tag == "button"
                and element.attrs.get("name") == pressing
                and any(ancestor is form for ancestor in element.ancestors)
                for element in inside
            )
        ]
    (form,) = forms
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
        _submit(self.client, self.page(), "data-agent", AGENT, pressing="blocked")
        self.assertIsNotNone(AgentIdentity.objects.get(client_id=AGENT).blocked_at)
        self.assertTrue(select(self.page(), "data-blocked"))

        _submit(self.client, self.page(), "data-agent", AGENT, pressing="blocked")
        self.assertIsNone(AgentIdentity.objects.get(client_id=AGENT).blocked_at)

    def test_a_refusal_for_calling_too_much_is_on_its_row(self):
        denials.record_denial(interface="mcp", reason="rate_limited", actor=AGENT)

        row = " ".join(element.words for element in within(self.page(), "data-agent", AGENT))

        self.assertIn("refused", row)
        self.assertIn("calling too much", row)

    def test_a_machines_whole_grant_is_in_force_on_its_row(self):
        granted = ("manage_contacts", "read", "read_audit_log", "write_projects")
        agent_registry.observe(agent_principal("example-machine", "mcp", frozenset(granted)))

        row = within(self.page(), "data-agent", "example-machine")

        named = {element.words for element in row if element.tag == "code"} - {"example-machine"}
        self.assertEqual(sorted(named), list(granted))
        self.assertIn("4 of 4", " ".join(element.words for element in row if element.tag == "td"))


# Written out, so the module imports at a base without them.
CLIENT_SETUP = "#setting-up-a-client"
ADDING_AN_AGENT = "setup"
ROOT = Path(__file__).resolve().parents[4]


class MissingPermissionTests(TestCase):
    """An agent a person signed in, named with what HQ offers that its last
    sign-in did not carry and the step that fixes it, before a sign-in asking
    for all of them is refused."""

    def setUp(self):
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        roles.assign(self.person, roles.Role.OPERATOR)
        self.client.force_login(self.person)

    def signed_in(self, granted, person="mine"):
        AgentIdentity.objects.create(
            client_id=AGENT,
            interfaces=["mcp"],
            granted=sorted(granted),
            person=self.person if person == "mine" else person,
        )
        return self.client.get(reverse("agent_policy"))

    def test_a_permission_its_sign_in_did_not_carry_is_named_with_its_fix(self):
        left_out = str(Capability.LOOK_UP_PUBLIC_RECORDS)

        page = self.signed_in(agent_grantable() - {left_out})

        shown = select(page, "data-missing")
        said = " ".join(element.words for element in shown)
        inside = within(page, "data-missing")
        named = {element.words for element in inside if element.tag == "code"} - {AGENT}
        self.assertEqual(named, {left_out})
        self.assertIn(AGENT, {element.words for element in inside if element.tag == "code"}, "whom to grant it to")
        self.assertIn("sign-in service", said)
        self.assertIn("sign it in again", said)
        hrefs = [element.attrs["href"] for element in inside if element.tag == "a"]
        self.assertTrue(any(href.endswith(CLIENT_SETUP) for href in hrefs), hrefs)
        self.assertTrue(all(any(a.attrs.get("data-agent") == AGENT for a in each.ancestors) for each in shown))

    def test_an_agent_missing_nothing_shows_nothing_more(self):
        page = self.signed_in(agent_grantable())

        self.assertFalse(select(page, "data-missing"))
        self.assertNotIn("not granted", " ".join(element.words for element in within(page, "data-agent", AGENT)))

    def test_the_devices_permission_is_never_missing_for_an_agent(self):
        page = self.signed_in(agent_grantable() - {DEVICE_SCOPE})

        self.assertFalse(select(page, "data-missing"))
        self.assertNotIn(DEVICE_SCOPE, text(page))

    def test_a_machines_own_token_is_never_told_to_sign_in_again(self):
        page = self.signed_in({"read"}, person=None)

        self.assertFalse(select(page, "data-missing"))

    def test_nothing_its_persons_role_lacks_is_named(self):
        with no_default_role():
            viewer = get_user_model().objects.create_user("riley")
        roles.assign(viewer, roles.Role.VIEWER)

        # Every permission offered that a viewer holds, whatever the installed
        # set offers: what is left out is only what the role lacks.
        page = self.signed_in(agent_grant(agent_grantable(), roles.grants(roles.Role.VIEWER)), person=viewer)

        self.assertFalse(select(page, "data-missing"))


class AddingAnAgentTests(TestCase):
    """The steps for adding an agent: one copy, on the page."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))

    def steps(self, page):
        (fold,) = select(page, "id", ADDING_AN_AGENT)
        return fold, [element for element in within(page, "id", ADDING_AN_AGENT) if element.tag == "li"]

    def test_they_are_on_the_page_and_point_at_how_the_permissions_are_written(self):
        _seen(AGENT)
        page = self.client.get(reverse("agent_policy"))

        fold, steps = self.steps(page)
        self.assertTrue(steps)
        self.assertNotIn("open", fold.attrs, "folded once an agent is there")
        hrefs = [element.attrs["href"] for element in within(page, "id", ADDING_AN_AGENT) if element.tag == "a"]
        self.assertTrue(any(href.endswith(CLIENT_SETUP) for href in hrefs), hrefs)

    def test_with_no_agent_yet_they_are_open(self):
        fold, steps = self.steps(self.client.get(reverse("agent_policy")))

        self.assertIn("open", fold.attrs)
        self.assertTrue(steps)


@tag("source")
class AddingAnAgentDocsTests(SimpleTestCase):
    """docs/API.md points at the page's steps, and the page's link lands on a heading there."""

    def test_the_docs_point_at_the_steps_and_their_link_lands_on_a_heading(self):
        from hq.platform.application.agent_activity import CLIENT_SETUP as linked

        guide = (ROOT / "docs" / "API.md").read_text()
        headings = {
            re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
            for heading in re.findall(r"^#+ (.+)$", guide, re.MULTILINE)
        }
        self.assertIn(linked.rpartition("#")[2], headings)
        self.assertIn(f"/agents/#{ADDING_AN_AGENT}", guide)


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


# Written out, so the module imports at a base without them.
WAITING = "wants to act for you from"
LAPTOP, LAPTOP_ID, LAPTOP_OWNER = "example-laptop", "nLAPTOP7CNTRL", "op@example.com"
# HQ's own loopback: nowhere the tailnet places a call.
UNPLACED_AT = "127.0.0.1"


class AgentDeviceTests(TestCase):
    """Each device an agent called from, under its agent: what it is, where it
    stands, and the controls the person reading may press."""

    def setUp(self):
        self.person = get_user_model().objects.create_user("op", password="x" * 20)
        LinkedAccount.objects.create(user=self.person, provider=SIGN_IN, login="example-key")
        self.client.force_login(self.person)
        self.identity = AgentIdentity.objects.create(
            client_id=AGENT, person_key="example-key", interfaces=["mcp"], person=self.person
        )

    def device(self, person="mine", **state):
        owner = self.person if person == "mine" else person
        return AgentDevice.objects.create(
            identity=self.identity,
            person=owner,
            node_id=LAPTOP_ID,
            name=LAPTOP,
            os="macOS",
            owner=LAPTOP_OWNER,
            **state,
        )

    def page(self):
        return self.client.get(reverse("agent_policy"))

    def standing(self, device):
        (row,) = select(self.page(), "id", f"device-{device.pk}")
        return row.attrs["data-device-standing"]

    def test_a_device_awaiting_the_reader_asks_them_by_the_agent_device_and_owner(self):
        device = self.device()

        (prompt,) = select(self.page(), "data-prompt")

        self.assertEqual(prompt.attrs["data-prompt"], f"device-{device.pk}")
        said = " ".join(element.words for element in within(self.page(), "data-prompt", f"device-{device.pk}"))
        self.assertIn(f"{AGENT} {WAITING} {LAPTOP}, owned by {LAPTOP_OWNER}", said)

    def test_approved_from_its_prompt_it_stops_asking(self):
        device = self.device()

        _submit(self.client, self.page(), "data-prompt", f"device-{device.pk}", pressing="approved")

        device.refresh_from_db()
        self.assertEqual(device.approved_by, self.person)
        self.assertEqual(self.standing(device), "approved")
        self.assertFalse(select(self.page(), "data-prompt"))

    def test_a_device_is_blocked_from_its_row_and_then_allowed(self):
        device = self.device(approved_at=timezone.now(), approved_by=self.person)
        rows = self.page()

        self.client.post(reverse("agent_device"), {"device": device.pk, "blocked": "1", **self.retry_key(rows)})
        self.assertEqual(self.standing(device), "blocked")
        _submit(self.client, self.page(), "id", f"device-{device.pk}")

        self.assertEqual(self.standing(device), "approved")

    def test_a_device_s_control_returns_to_its_row(self):
        device = self.device(approved_at=timezone.now(), approved_by=self.person)

        response = _submit(self.client, self.page(), "id", f"device-{device.pk}", pressing="blocked")

        self.assertEqual(response.url, f"{reverse('agent_policy')}#device-{device.pk}")

    def retry_key(self, page):
        key = select(page, "name", "__retry_key")[0]
        return {"__retry_key": key.attrs["value"]}

    def test_another_persons_device_waits_for_them_and_offers_the_reader_only_its_block(self):
        other = get_user_model().objects.create_user("riley")
        device = self.device(person=other)
        page = self.page()

        self.assertFalse(select(page, "data-prompt"))
        row = within(page, "id", f"device-{device.pk}")
        offered = {element.attrs["name"] for element in row if element.tag == "button" and element.attrs.get("name")}
        self.assertEqual(offered, {"blocked"})
        self.assertIn("riley", " ".join(element.words for element in row))

    def test_a_machine_agents_device_asks_anyone_who_governs_agents(self):
        machine = AgentIdentity.objects.create(client_id="example-machine", interfaces=["api"])
        device = AgentDevice.objects.create(identity=machine, node_id="nSERVER1CNTRL", name="example-server")

        said = " ".join(element.words for element in within(self.page(), "data-prompt", f"device-{device.pk}"))

        self.assertIn("example-machine wants to act from example-server", said)

    def test_an_address_no_device_sent_is_on_the_page_as_refused_with_nothing_to_press(self):
        with operation_context(interface="mcp", actor=AGENT, operation="example"):
            denials.record_denial(interface="mcp", reason="device_unknown", actor=AGENT, source=UNPLACED_AT)

        page = self.page()

        (row,) = select(page, "data-device-standing", "unplaceable")
        self.assertIn(UNPLACED_AT, row.words)
        inside = within(page, "id", row.attrs["id"])
        self.assertTrue([element for element in inside if "data-unplaced" in element.attrs], "why it was refused")
        self.assertFalse(
            [element for element in inside if element.attrs.get("type") == "submit" or element.tag == "form"],
            "nothing can be approved",
        )

    def test_the_page_says_only_when_calls_are_tied_to_no_device(self):
        (none,) = select(self.page(), "data-placement")
        with override_settings(SEVERINO_TAILSCALED_SOCKET="/run/tailscale/tailscaled.sock"):
            placed = select(self.page(), "data-placement")

        self.assertEqual(none.attrs["data-placement"], "none")
        self.assertEqual(placed, [], "a tailnet device placing each call is the normal case, said by the Devices table")

    def test_a_devices_calls_are_counted_on_its_row(self):
        device = self.device(approved_at=timezone.now(), approved_by=self.person)
        for _ in range(3):
            AuditLog.objects.create(
                action=AuditLog.Action.UPDATED, metadata={"actor": AGENT, "interface": "mcp", "device": LAPTOP_ID}
            )

        row = " ".join(element.words for element in within(self.page(), "id", f"device-{device.pk}"))

        self.assertIn("3 calls", row)

    def test_a_refusal_from_a_device_points_at_its_row(self):
        device = self.device()
        agent = Principal(AGENT, "mcp", frozenset({"read"}), person_key="example-key", device=LAPTOP_ID)

        refused = agent_access.refusal(agent)

        self.assertEqual(refused.code, "device_awaiting_approval")
        self.assertIn(f"{reverse('agent_policy')}#device-{device.pk}", refused.reason)
        self.assertTrue(select(self.page(), "id", f"device-{device.pk}"))

    def test_only_the_person_it_acts_for_may_approve_it(self):
        other = get_user_model().objects.create_user("riley")
        device = self.device(person=other)
        page = self.page()

        response = self.client.post(
            reverse("agent_device"), {"device": device.pk, "approved": "1", **self.retry_key(page)}
        )

        device.refresh_from_db()
        self.assertIsNone(device.approved_at)
        self.assertEqual(response.status_code, 403)


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
        self.assertNotIn("Your apps", said)
        standings.assert_not_called()

    def test_without_script_the_address_asks_for_it(self):
        with mock.patch("hq.platform.mcp.listing.endpoint", return_value=ENDPOINT):
            page = self.client.get(reverse("agent_policy"), {"endpoint": 1})

        self.assertIn("example_tool", text(page))
        self.assertIn("Your apps", text(page))

    def test_with_no_api_resource_it_says_agents_cannot_connect(self):
        with self.settings(SEVERINO_API_RESOURCE=""):
            part = self.client.get(reverse("agent_policy"), **PART)

        self.assertIn("cannot connect", text(part))

    def test_the_server_s_instructions_open_from_their_button_and_nowhere_else(self):
        told = replace(ENDPOINT, instructions="Start with `hq://hq`.\n\nThen `example_next`.")
        with mock.patch("hq.platform.mcp.listing.endpoint", return_value=told):
            part = self.client.get(reverse("agent_policy"), **PART)

        (opener,) = select(part, "command", "show-modal")
        (dialog,) = select(part, "id", opener.attrs["commandfor"])
        self.assertEqual((opener.tag, dialog.tag), ("button", "dialog"))
        self.assertIn("aria-labelledby", dialog.attrs)
        inside = within(part, "id", dialog.attrs["id"])
        self.assertLessEqual(
            {"hq://hq", "example_next"}, {element.words for element in inside if element.tag == "code"}
        )
        paragraphs = [element for element in elements(part, "p") if "example_next" in element.words]
        self.assertTrue(paragraphs)
        self.assertTrue(all(dialog in element.ancestors for element in paragraphs))
