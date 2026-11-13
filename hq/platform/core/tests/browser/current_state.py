"""A current or chosen item carries the ARIA state that says so, in a real
browser, so a screen reader hears which one it is. That the stylesheet draws
the mark from that state is a written rule (Design.md)."""

import re
from html import unescape
from typing import override
from urllib.parse import urlsplit

from django.db import transaction
from django.test import Client
from django.urls import reverse

from hq.platform.application.action_links import topology_url
from hq.platform.core.tests.browser.gate import BrowserGate, answered, with_script
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import WIDE, viewport
lazy from hq.platform.core import seeds
lazy from hq.platform.core.example_estate import REQUEST
lazy from hq.platform.core.example_estate.testing import as_production, sign_in

_NODE = re.compile(
    r'data-topology-node="([^"]+)"\s+data-topology-node-kind="[^"]*"\s+data-topology-neighbors="([^"]*)"'
)
_BODY = re.compile(r'data-topology-node="([^"]+)"[^>]*?data-topology-body="([^"]+)"', re.DOTALL)
_XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

# In each group, the number of its items and of those that, or whose contents,
# carry the state named.
_MARKED = """([groups, items, marked]) => [...document.querySelectorAll(groups)].map((group) => {
  const found = [...group.querySelectorAll(items)];
  return [found.length, found.filter((item) => item.matches(marked) || item.querySelector(marked)).length];
})"""
_COLUMNS = """(detail) => getComputedStyle(document.querySelector(detail).parentElement)
  .gridTemplateColumns.split(" ").length"""
_SHOWN = "(selector) => getComputedStyle(document.querySelector(selector)).display !== 'none'"


class CurrentStateBrowserTests(BrowserGate):
    """The topology's trace chips, its selected node and its details, the
    command center's chosen option and the header's current section, each
    marked by the ARIA attribute that states it, on the example estate."""

    # The node the page opens on, one it links to, and the map's address.
    focused: str
    neighbor: str
    map: str

    @classmethod
    @override
    def render(cls):
        answers = {}
        with transaction.atomic(), as_production(), seeds.built("showcase") as owner:
            client = Client()
            sign_in(client, owner)

            def ask(address, **headers):
                response = client.get(address, secure=True, follow=True, **REQUEST, **headers)
                if response.status_code != 200:
                    raise AssertionError(f"{address} answered {response.status_code}")
                return answered(response)

            cls.map = reverse("control_plane:topology")
            whole = ask(cls.map)
            bodies = {node: unescape(url) for node, url in _BODY.findall(whole)}
            # A card's neighbors include nodes the whole map leaves out
            # (kept_on_the_map), which have no card to select: the pair is
            # one the map draws both ends of.
            cls.focused, cls.neighbor = next(
                (node, other) for node, linked in _NODE.findall(whole) for other in linked.split() if other in bodies
            )
            answers[cls.map] = ask(topology_url(cls.focused))
            answers[urlsplit(bodies[cls.neighbor]).path] = ask(bodies[cls.neighbor], **_XHR)
            answers[reverse("search")] = ask(reverse("search"), HTTP_X_FRAGMENT="palette", **_XHR)
            transaction.set_rollback(True)
        return answers

    @override
    def respond(self, route):
        path = urlsplit(route.request.url).path
        if not self.holds(route) and path in self.pages:
            return self.fulfill(route, self.pages[path])
        return super().respond(route)

    def open_map(self):
        self.page.set_viewport_size(viewport(WIDE))
        self.page.goto(f"{ORIGIN}{topology_url(self.focused)}", wait_until="load")

    def marks(self, items, marked="[aria-current]", groups="html"):
        """Each group's count of items and of those marked, as ``_MARKED`` reads them; the whole page is one group."""

        found = self.page.evaluate(_MARKED, [groups, items, marked])
        return found if groups != "html" else found[0]

    def assert_marked(self, marks, marked=1):
        """Exactly ``marked`` items carry the state, beside others that do not."""

        count, carrying = marks
        self.assertEqual(carrying, marked)
        self.assertGreater(count, marked, "nothing to tell the marked item from")

    def test_the_trace_s_current_chip_and_the_header_s_current_section_are_marked(self):
        self.open_map()
        groups = self.marks(SELECTORS["trace_chip"], groups=SELECTORS["trace_controls"])
        self.assertEqual(len(groups), 2, "a trace offers its direction and its steps")
        for at, group in enumerate(groups):
            with self.subTest(group=at):
                self.assert_marked(group)
        self.assert_marked(self.marks(SELECTORS["nav_entry"]))

    def test_the_node_the_page_opens_on_is_marked_with_its_details_beside_it(self):
        self.open_map()
        self.assert_marked(self.marks(SELECTORS["map_node"]))
        self.assertEqual(self.page.evaluate(_COLUMNS, SELECTORS["map_detail"]), 2)

    @with_script
    def test_the_mark_moves_with_the_node_selected_and_leaves_with_its_details(self):
        self.open_map()
        select = self.page.locator(f'[data-topology-node="{self.neighbor}"]').locator(SELECTORS["map_select"])
        select.click()
        self.page.wait_for_function(
            '(id) => document.querySelector(`[data-topology-node="${id}"] [aria-current]`)', arg=self.neighbor
        )
        self.assert_marked(self.marks(SELECTORS["map_node"]))
        self.assertEqual(self.page.evaluate(_COLUMNS, SELECTORS["map_detail"]), 2)

        # Selecting the selected node clears it, and its details go with it.
        select.click()
        self.page.wait_for_function("(detail) => document.querySelector(detail).hidden", arg=SELECTORS["map_detail"])
        self.assert_marked(self.marks(SELECTORS["map_node"]), marked=0)
        self.assertEqual(self.page.evaluate(_COLUMNS, SELECTORS["map_detail"]), 1)

    @with_script
    def test_the_command_center_marks_the_option_enter_opens(self):
        self.open_map()
        self.page.keyboard.press("Control+k")
        options = self.page.locator(SELECTORS["palette_option"])
        options.first.wait_for(state="visible")
        self.assertTrue(self.page.evaluate(_SHOWN, SELECTORS["palette_hint"]))

        self.page.locator(SELECTORS["palette_input"]).press("ArrowDown")
        self.assert_marked(self.marks(SELECTORS["palette_option"], '[aria-selected="true"]'))
        self.assertFalse(self.page.evaluate(_SHOWN, SELECTORS["palette_hint"]))

        self.page.locator(SELECTORS["palette_input"]).press("ArrowUp")
        self.assert_marked(self.marks(SELECTORS["palette_option"], '[aria-selected="true"]'), marked=0)
        self.assertTrue(self.page.evaluate(_SHOWN, SELECTORS["palette_hint"]))
