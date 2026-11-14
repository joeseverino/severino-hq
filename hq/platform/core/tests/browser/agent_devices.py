"""The Agents page in a real browser: a device waiting for the person is
approved from its prompt, then blocked from its row, each press answered by
the page HQ draws after it; and each agent's open update streams said in its
row's detail, warned of in the row only at the cap."""

from typing import override
from urllib.parse import parse_qs, urlsplit

from django.db import transaction
from django.test import Client
from django.urls import reverse

from hq.platform.application.capabilities import capability_words
from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key
from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
lazy from hq.platform.application.agent_devices import APPROVE_AGENT_DEVICE, BLOCK_AGENT_DEVICE, device_anchor
lazy from hq.platform.core import seeds
lazy from hq.platform.core.example_estate import REQUEST
lazy from hq.platform.core.example_estate.agents import AGENT, ASSISTANT
lazy from hq.platform.core.example_estate.testing import as_production, sign_in
lazy from hq.platform.core.models import AgentDevice


class AgentDeviceBrowserTests(BrowserGate):
    """Approving and blocking an agent's device, on the example estate."""

    scripted = True
    # The page, the device waiting for the owner, and what HQ answers each press with.
    page_path: str
    anchor: str
    device: str
    after: dict[str, object]

    @classmethod
    @override
    def render(cls):
        with transaction.atomic(), as_production(), seeds.built("showcase") as owner:
            client = Client()
            sign_in(client, owner)
            cls.page_path = reverse("agent_policy")
            pk = AgentDevice.objects.get(identity__client_id=ASSISTANT, person=owner).pk
            cls.device, cls.anchor = str(pk), device_anchor(pk)
            before = answered(client.get(cls.page_path, secure=True, **REQUEST))
            cls.after = {}
            for field in ("approved", "blocked"):
                pressed = client.post(
                    reverse("agent_device"),
                    {"device": cls.device, field: "1", "next": cls.page_path, RETRY_KEY_FIELD: new_key()},
                    secure=True,
                    follow=True,
                    **REQUEST,
                )
                if pressed.status_code != 200:
                    raise AssertionError(f"pressing {field} answered {pressed.status_code}")
                cls.after[field] = answered(pressed)
            transaction.set_rollback(True)
        return {cls.page_path: before}

    @override
    def start(self, **options):
        super().start(**options)
        self.posted = []

    @override
    def respond(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method == "POST" and path == reverse("agent_device"):
            posted = parse_qs(request.post_data or "")
            self.posted.append(posted)
            (field,) = [name for name in self.after if name in posted]
            return self.fulfill(route, self.after[field])
        if path == self.page_path and not self.holds(route):
            return self.fulfill(route, self.pages[self.page_path])
        return super().respond(route)

    def row(self):
        return self.page.locator(f"#{self.anchor}")

    def test_a_waiting_device_is_approved_from_its_prompt_then_blocked_from_its_row(self):
        self.page.goto(f"{ORIGIN}{self.page_path}", wait_until="load")
        self.assertEqual(self.row().get_attribute("data-device-standing"), "awaiting")

        prompt = self.page.locator(SELECTORS["device_prompt"])
        self.assertEqual(prompt.get_attribute("data-prompt"), self.anchor)
        with self.page.expect_navigation():
            prompt.get_by_role("button", name=capability_words(APPROVE_AGENT_DEVICE, "on")).click()

        approved = self.posted[-1]
        self.assertEqual((approved.get("device"), approved.get("approved")), ([self.device], ["1"]))
        self.assertTrue(approved["next"][0].endswith(f"#{self.anchor}"), "the press returns to the device's row")
        self.assertEqual(self.row().get_attribute("data-device-standing"), "approved")
        self.assertEqual(self.page.locator(SELECTORS["device_prompt"]).count(), 0, "nothing waits for the owner now")

        with self.page.expect_navigation():
            self.row().get_by_role("button", name=capability_words(BLOCK_AGENT_DEVICE, "on")).click()

        blocked = self.posted[-1]
        self.assertEqual((blocked.get("device"), blocked.get("blocked")), ([self.device], ["1"]))
        self.assertEqual(self.row().get_attribute("data-device-standing"), "blocked")
        self.assertEqual(self.row().get_by_role("button", name=capability_words(BLOCK_AGENT_DEVICE, "off")).count(), 1)

    def test_a_full_set_of_streams_is_warned_of_in_its_row_and_open_ones_are_said_in_the_detail(self):
        self.page.goto(f"{ORIGIN}{self.page_path}", wait_until="load")

        def drawn(selector):
            # A row names its agent as client@person, so the client is what is
            # compared; the row's open line is the row after it.
            agent = "element => (element.closest('[data-agent]') ?? element.closest('tr').previousElementSibling)"
            return {
                element.evaluate(f"{agent}.dataset.agent").partition("@")[0]: (element.get_attribute("data-streams"))
                for element in self.page.locator(selector).all()
            }

        self.assertEqual(drawn(SELECTORS["agent_streams"]), {ASSISTANT: "full"}, "only a full set is in the row's cell")
        self.assertEqual(
            drawn(SELECTORS["agent_streams_detail"]),
            {AGENT: "open"},
            "an agent holding none shows nothing more",
        )
        for element in self.page.locator(SELECTORS["agent_streams"]).all():
            self.assertTrue(element.is_visible(), "a full set shows without opening the row")
