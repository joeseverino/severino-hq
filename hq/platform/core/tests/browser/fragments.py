"""Fragments in a real browser: a region fetched, refreshed and polled as fragment.js answers it."""

from typing import override
from urllib.parse import parse_qsl, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.template import Context, Template
from django.test import Client
from django.urls import reverse

from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.probes.settle import SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import NONCE, ORIGIN

# A region that polls and names its parts, a disclosure and a menu, a
# placeholder behind a closed disclosure, and one whose address answers a
# whole page.
_FRAGMENT_PROBE = f"""<!doctype html><html><head><title>Probe</title>
<script defer src="{settings.STATIC_URL}js/fragment.js" nonce="{NONCE}"></script>
</head><body><main>
<section data-fragment="/probe-strip/" data-fragment-poll="1">
<details data-fragment-part="first"><summary>First</summary>as drawn</details>
<div data-fragment-part="second" data-menu><button popovertarget="second-panel">Second</button><div id="second-panel" popover>as drawn</div></div>
</section>
<details data-probe="fold"><summary>More</summary>
<div data-fragment="/probe-slot/" data-fragment-load data-fragment-failure="Could not be read.">waiting</div>
</details>
<div data-fragment="/probe-page/" data-fragment-load data-fragment-failure="Could not be read.">waiting</div>
</main></body></html>"""

_PROBE_STRIP = """<section data-fragment="/probe-strip/"{poll}>
<details data-fragment-part="first"><summary>First</summary>as drawn</details>
<div data-fragment-part="second" data-menu><button popovertarget="second-panel">Second</button><div id="second-panel" popover>read again</div></div>
</section>"""

# A form answered in place whose submitting button shares a field's name,
# between that field and another.
_FORM_PROBE = f"""<!doctype html><html><head><title>Probe</title>
<script defer src="{settings.STATIC_URL}js/fragment.js" nonce="{NONCE}"></script>
</head><body><main>
<form action="/probe-sent/" data-fragment-target="sent">
<input name="key" value="field">
<button name="key" value="button">Send</button>
<input name="note" value="last">
</form>
<div id="sent" data-fragment="/probe-sent/">waiting</div>
</main></body></html>"""

# A region that asks to add its answers to history, which nothing restores.
_PUSH_PROBE = f"""<!doctype html><html><head><title>Probe</title>
<script defer src="{settings.STATIC_URL}js/fragment.js" nonce="{NONCE}"></script>
</head><body><main>
<div id="pushed" data-fragment data-fragment-links data-fragment-history="push">
<a data-probe="push" href="/probe-push/?turn=2">Next</a>
</div>
</main></body></html>"""


def _dialog_probe():
    """The shared modal holding back a placeholder until it opens, opened by
    the platform's own command so no script of HQ's opens it."""

    slot = Template('<div data-fragment="/probe-slot/" data-fragment-load>waiting</div>')
    dialog = Template('{% include "partials/_modal.html" with id="probe" title="Probe" body_template=slot %}').render(
        Context({"slot": slot})
    )
    return f"""<!doctype html><html><head><title>Probe</title>
<script defer src="{settings.STATIC_URL}js/fragment.js" nonce="{NONCE}"></script>
</head><body><main>
<button type="button" data-probe="open" command="show-modal" commandfor="modal-probe">Open</button>
{dialog}
</main></body></html>"""


_PROBE_STATE = """(failure) => ({
  first: document.querySelector('[data-fragment-part="first"]').kept === true,
  firstOpen: document.querySelector('[data-fragment-part="first"]').open,
  second: document.querySelector('[data-fragment-part="second"]').textContent,
  secondOpen: document.querySelector('[data-fragment-part="second"] [popover]').matches(':popover-open'),
  polling: document.querySelector('[data-fragment-poll]') !== null,
  slot: document.querySelector('[data-probe="fold"]').textContent,
  page: [...document.querySelectorAll(failure)].map((line) => line.textContent),
  titles: document.querySelectorAll('main title, main main').length,
})"""


class FragmentBrowserTests(BrowserGate):
    """The fragment primitive, in a browser: what it swaps, keeps and stops."""

    scripted = True
    # The calendar's month, and the month after it.
    calendar: str
    following: str

    @classmethod
    @override
    def render(cls):
        with transaction.atomic():
            client = Client()
            client.force_login(get_user_model().objects.create_superuser(username="operator"))
            url = reverse("calendar:month")
            page = client.get(url)
            following = page.context["next_url"]
            cls.calendar, cls.following = url, following
            answers = {
                (url, ""): answered(page),
                (following, "calendar"): answered(client.get(following, headers={"X-Fragment": "calendar"})),
                (reverse("action_item_count"), ""): answered(client.get(reverse("action_item_count"))),
            }
            transaction.set_rollback(True)
        return answers

    @override
    def start(self, **options):
        super().start(**options)
        self.asked = []
        self.strip = [' data-fragment-poll="1"', ""]

    @override
    def respond(self, route):
        request = route.request
        parts = urlsplit(request.url)
        address = parts.path + (f"?{parts.query}" if parts.query else "")
        part = request.headers.get("x-fragment", "")
        self.asked.append((address, part))
        if parts.path == "/probe/":
            return self.fulfill(route, _FRAGMENT_PROBE)
        if parts.path == "/probe-strip/":
            poll = self.strip.pop(0) if self.strip else ""
            return self.fulfill(route, _PROBE_STRIP.format(poll=poll))
        if parts.path == "/probe-slot/":
            return self.fulfill(route, "<p>read when opened</p>")
        if parts.path == "/probe-page/":
            return self.fulfill(route, _FRAGMENT_PROBE)
        if parts.path == "/probe-form/":
            return self.fulfill(route, _FORM_PROBE)
        if parts.path == "/probe-sent/":
            return self.fulfill(route, '<div id="sent" data-fragment="/probe-sent/">sent</div>')
        if parts.path == "/probe-push/":
            return self.fulfill(route, _PUSH_PROBE)
        if parts.path == "/probe-dialog/":
            return self.fulfill(route, _dialog_probe())
        if (address, part) in self.pages:
            return self.fulfill(route, self.pages[(address, part)])
        return super().respond(route)

    def test_a_polled_region_takes_only_what_changed_and_stops_when_told(self):
        self.page.clock.install()
        self.page.goto(f"{ORIGIN}/probe/", wait_until="load")
        self.page.evaluate(
            "() => { const first = document.querySelector('[data-fragment-part=\"first\"]');"
            " first.kept = true; first.open = true;"
            " document.querySelector('[data-fragment-part=\"second\"] [popovertarget]').click(); }"
        )
        # Its first turn reads it again and keeps polling; the second is told to stop.
        self.page.clock.run_for(1000)
        self.page.wait_for_function(
            "() => document.querySelector('[data-fragment-part=\"second\"]').textContent.includes('read again')"
        )
        self.page.clock.run_for(1000)
        self.page.wait_for_function("() => document.querySelector('[data-fragment-poll]') === null")
        state = self.page.evaluate(_PROBE_STATE, SELECTORS["fragment_failure"])

        # The part that did not change is the same node, still open; the one
        # that did was replaced, its menu open as the reader left it.
        self.assertTrue(state["first"])
        self.assertTrue(state["firstOpen"])
        self.assertEqual(state["second"].strip(), "Secondread again")
        self.assertTrue(state["secondOpen"])
        asked = self.asked.count(("/probe-strip/", ""))
        self.page.clock.run_for(5000)
        # A request after those turns is answered after any they made.
        self.page.evaluate("() => fetch('/probe-slot/')")
        self.assertEqual(self.asked.count(("/probe-strip/", "")), asked)

    def test_a_placeholder_waits_for_its_disclosure_and_refuses_a_whole_page(self):
        self.page.goto(f"{ORIGIN}/probe/", wait_until="load")
        self.page.wait_for_selector(SELECTORS["fragment_failure"])
        state = self.page.evaluate(_PROBE_STATE, SELECTORS["fragment_failure"])

        self.assertNotIn(("/probe-slot/", ""), self.asked)
        self.assertEqual(state["page"], ["Could not be read."])
        self.assertEqual(state["titles"], 0)

        self.page.locator(SELECTORS["fragment_fold"]).click()
        self.page.wait_for_function(
            "() => document.querySelector('[data-probe=\"fold\"]').textContent.includes('read when opened')"
        )
        self.assertEqual(self.asked.count(("/probe-slot/", "")), 1)

    def test_a_form_answered_in_place_sends_what_a_plain_submit_sends(self):
        self.page.goto(f"{ORIGIN}/probe-form/", wait_until="load")
        self.page.evaluate("() => document.querySelector('button[name=\"key\"]').click()")
        self.page.wait_for_function("() => document.getElementById('sent').textContent === 'sent'")

        sent = [urlsplit(address).query for address, _part in self.asked if address.startswith("/probe-sent/?")]
        # The button among the fields in tree order, beside the field of its name.
        self.assertEqual(
            [parse_qsl(query) for query in sent], [[("key", "field"), ("key", "button"), ("note", "last")]]
        )

    def test_a_dialog_reads_what_it_holds_back_when_it_opens_and_closes_by_its_own_button(self):
        self.page.goto(f"{ORIGIN}/probe-dialog/", wait_until="load")
        self.page.evaluate(SETTLE)
        self.assertNotIn(("/probe-slot/", ""), self.asked)

        self.page.evaluate("() => document.querySelector('[data-probe=\"open\"]').click()")
        self.page.wait_for_function("() => document.querySelector('dialog').textContent.includes('read when opened')")
        self.page.evaluate("() => document.querySelector('dialog [aria-label=\"Close\"]').click()")
        self.page.wait_for_function("() => !document.querySelector('dialog').open")
        self.assertEqual(self.asked.count(("/probe-slot/", "")), 1)

    def test_paging_the_calendar_asks_for_its_part_and_keeps_the_page(self):
        self.page.goto(f"{ORIGIN}{self.calendar}", wait_until="load")
        was = self.page.locator(SELECTORS["calendar_period"]).text_content()
        self.page.evaluate("() => { window.kept = true; }")
        self.page.locator(SELECTORS["calendar_next"]).focus()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function(
            "([period, was]) => document.querySelector(period).textContent !== was",
            arg=[SELECTORS["calendar_period"], was],
        )

        self.assertIn((self.following, "calendar"), self.asked)
        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertTrue(self.page.url.endswith(self.following))
        # The keyboard is where it was: on the control that was pressed.
        self.assertEqual(self.page.evaluate("() => document.activeElement.getAttribute('rel')"), "next")

    def test_a_region_that_would_add_to_history_is_asked_the_ordinary_way(self):
        """Back restores no region's own entry, so a region asking to push
        one has its links followed as links."""

        self.page.goto(f"{ORIGIN}/probe-push/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        with self.page.expect_navigation(wait_until="load"):
            self.page.evaluate("() => document.querySelector('[data-probe=\"push\"]').click()")
        self.assertTrue(self.page.url.endswith("/probe-push/?turn=2"))
        self.assertFalse(self.page.evaluate("() => window.kept === true"))
