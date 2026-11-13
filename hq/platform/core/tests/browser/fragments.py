"""Fragments in a real browser: a region fetched, refreshed and polled as fragment.js answers it."""

import re
from typing import override
from urllib.parse import parse_qsl, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.template import Context, Template
from django.test import Client
from django.urls import reverse

from hq.platform.application.pages import PageVisit
from hq.platform.core.tests.browser.gate import BrowserGate, answered, sent
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


# The probes whose answer never changes, by their address.
_FIXED = {
    "/probe/": _FRAGMENT_PROBE,
    "/probe-slot/": "<p>read when opened</p>",
    "/probe-page/": _FRAGMENT_PROBE,
    "/probe-form/": _FORM_PROBE,
    "/probe-sent/": '<div id="sent" data-fragment="/probe-sent/">sent</div>',
    "/probe-push/": _PUSH_PROBE,
}


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


# A dialog's panel, fetched from its own address when the dialog opens: first
# as last read, with the ask a page about one thing draws; then, once that
# read has landed, as it is now and with nothing left to ask.
_PANEL = '<div data-probe="panel" data-fragment="/probe-panel/">{ask}<p>{said}</p></div>'


def _asking_dialog_probe():
    ask = Template(
        '{% include "partials/_visit_refresh.html" with subject="page" name="example" refresh="[data-probe=panel]" %}'
    ).render(Context({}))
    slot = Template('<div data-fragment="/probe-panel/" data-fragment-load>waiting</div>')
    dialog = Template('{% include "partials/_modal.html" with id="probe" title="Probe" body_template=slot %}').render(
        Context({"slot": slot})
    )
    return (
        f"""<!doctype html><html><head><title>Probe</title>
<script defer src="{settings.STATIC_URL}js/fragment.js" nonce="{NONCE}"></script>
<script defer src="{settings.STATIC_URL}js/ask.js" nonce="{NONCE}"></script>
</head><body><main>
<button type="button" data-probe="open" command="show-modal" commandfor="modal-probe">Open</button>
{dialog}
</main></body></html>""",
        _PANEL.format(ask=ask, said="as last read"),
        _PANEL.format(ask="", said="as it is now"),
    )


# A page about one thing: the content draws its ask and the note beside it,
# first as last read and then as it is now.
_ASKING_HEAD = """<main class="content" id="content">
<header class="page-head"><h1>Probe</h1></header>
<p data-probe="said">{said}</p>
{control}
</main>"""


def _asking_head_probe():
    control = Template('{% include "partials/_visit_refresh.html" with visit=visit %}').render(
        Context({"visit": PageVisit("page", "example")})
    )
    scripts = f"""<!doctype html><html><head><title>Probe</title>
<script defer src="{settings.STATIC_URL}js/fragment.js" nonce="{NONCE}"></script>
<script defer src="{settings.STATIC_URL}js/ask.js" nonce="{NONCE}"></script>
</head><body>"""
    return [
        scripts + _ASKING_HEAD.format(said=said, control=control) + "</body></html>"
        for said in ("as last read", "as it is now")
    ]


# How the read a page asked for stands: under way, then landed.
_VISIT_LIVE = (
    '{"state": "queued", "live": true, "label": "Waiting", "note": "Waiting for a fresh reading.",'
    ' "status": "/probe-visit-status/", "seconds": 0}'
)
_VISIT_DONE = '{"state": "done", "live": false, "label": "Done", "note": "Refreshed just now.", "status": ""}'


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


# Rows a page lets the reader reorder and delete, each by its own
# post_button, drawn in a real page's frame: base.html's content, its one post
# form and its scripts. Between enough lines that the rows are reached by
# scrolling; beside them a region of its own with a press of its own, a press
# answered with no page and one the server fails, and a link and a form of
# the page's own.
_ROWS = """{% load action_tags %}{% if said %}<ul class="messages"><li class="msg">{{ said }}</li></ul>{% endif %}
<p data-probe="other">{{ other }}</p>
{% for line in lines %}<p>An example line {{ line }}.</p>{% endfor %}
<ul class="list-rows" data-probe="rows">{% for row in rows %}<li data-row="{{ row }}"><span>Example {{ row }}</span>
{% with url="/probe-rows/"|add:row|add:"/" %}
{% post_button "\u2191" url value="up" css="icon-link" title="Move up" disabled=forloop.first %}
{% post_button "\u2193" url value="down" css="icon-link" title="Move down" disabled=forloop.last %}
{% post_button "\u00d7" url value="delete" css="icon-link" title="Delete" %}
{% endwith %}</li>{% endfor %}</ul>
<div id="probe-aside" data-fragment><span data-probe="aside">{{ aside }}</span>
{% post_button "Mark" "/probe-rows/aside/" value="mark" %}</div>
{% post_button "Quiet" "/probe-rows/quiet/" value="quiet" %}
{% post_button "Broken" "/probe-rows/broken/" value="broken" %}
{% post_button "Refused" "/probe-rows/refused/" value="refused" %}
{% post_button "Declined" "/probe-rows/declined/" value="declined" %}
<a data-probe="link" href="/probe-rows/?again=1">Again</a>
<form method="post" action="/probe-rows/plain/"><button data-probe="plain">Send</button></form>
{% for line in lines %}<p>An example line after {{ line }}.</p>{% endfor %}"""
_MAIN = re.compile(r"(<main\b[^>]*>).*?(</main>)", re.DOTALL)

# Why the server refused a press: drawn among the messages of the page it
# sent the press on to, and as the whole answer to one it answered itself.
_REFUSED = "Example refused: its connection only reads."
_DECLINED = "Example declined: it needs a connection that manages."
# Another page, saying the refusal among its messages. Read again, it would
# not: a message is said once.
_ELSEWHERE = f"""<!doctype html><html><head><title>Elsewhere</title></head><body>
<main class="content" id="elsewhere"><ul class="messages"><li class="msg msg-error">{_REFUSED}</li></ul></main>
</body></html>"""

# What each press leaves of the rows.
_PRESSED = {("ABC", "C", "up"): "ACB", ("ABC", "B", "up"): "BAC", ("ABC", "B", "delete"): "AC"}

# The rows in the order the page shows them, and the page's messages: the
# list base.html draws first in the content.
_ROWS_SHOWN = "() => [...document.querySelectorAll('[data-row]')].map((row) => row.dataset.row).join('')"
_SAID = "() => [...document.querySelectorAll('main > ul:first-child > li')].map((line) => line.textContent.trim())"

# Where the keyboard is: which post_button, by where it posts and what it says.
_FOCUS = "() => [document.activeElement.getAttribute('formaction'), document.activeElement.value]"


def _posted(request):
    """A post's fields, sent by a form (urlencoded) or by a script (multipart)."""

    body = request.post_data or ""
    if "multipart/form-data" in (request.headers.get("content-type") or ""):
        return dict(re.findall(r'name="([^"]+)"\r\n\r\n([^\r]*)', body))
    return dict(parse_qsl(body))


def _rows_pages(frame):
    """The rows page in each state a press leaves it, in ``frame``'s page."""

    def draw(order, **values):
        content = Template(_ROWS).render(
            Context(
                {
                    "rows": list(order),
                    "lines": range(40),
                    "other": "An example part beside the rows.",
                    "aside": "Not marked",
                    **values,
                }
            )
        )
        return sent(_MAIN.sub(lambda m: m[1] + content + m[2], str(frame), count=1), frame.headers)

    pages = {
        order: draw(order, said="Example moved." if order != "ABC" else "") for order in ("ABC", *_PRESSED.values())
    }
    pages["marked"] = draw("ABC", aside="Marked", other="An example part drawn again elsewhere.")
    return pages


class FragmentBrowserTests(BrowserGate):
    """The fragment primitive, in a browser: what it swaps, keeps and stops."""

    scripted = True
    # The calendar's month, and the month after it.
    calendar: str
    following: str
    # The rows page in each state a press leaves it.
    rows: dict[str, str]

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
        cls.rows = _rows_pages(answers[(url, "")])
        return answers

    @override
    def start(self, **options):
        super().start(**options)
        self.asked = []
        self.strip = [' data-fragment-poll="1"', ""]
        self.asking, *self.panels = _asking_dialog_probe()
        self.heads = _asking_head_probe()
        self.visit = _VISIT_DONE
        self.order = "ABC"
        self.posted = []

    def answer_rows(self, route, parts):
        """The rows page. A press posts and is answered with the page as it
        then stands, where HQ redirects back to it: a route answers no
        redirect, and the page is found in either answer the same way."""

        request = route.request
        if request.method == "POST":
            fields = _posted(request)
            self.posted.append((parts.path, fields))
            row = parts.path.removeprefix("/probe-rows/").strip("/")
            if row == "broken":
                return route.fulfill(status=500, body="Example failure")
            if row == "refused":
                # Refused, and answered with another page that says why: what
                # a redirect elsewhere answers, which a route cannot send.
                return self.fulfill(route, _ELSEWHERE)
            if row == "declined":
                # Refused, answered with the sentence alone (``refusal_response``).
                return route.fulfill(status=409, content_type="text/plain; charset=utf-8", body=_DECLINED)
            if row == "quiet":
                # What a view gives a script that asks with nothing to show.
                return route.fulfill(status=204)
            if row == "aside":
                self.order = "marked"
            elif row != "plain":
                self.order = _PRESSED[(self.order, row, fields["action"])]
        return self.fulfill(route, self.rows[self.order])

    @override
    def respond(self, route):
        request = route.request
        parts = urlsplit(request.url)
        address = parts.path + (f"?{parts.query}" if parts.query else "")
        part = request.headers.get("x-fragment", "")
        self.asked.append((address, part))
        if parts.path.startswith("/probe-rows/"):
            return self.answer_rows(route, parts)
        if (fixed := _FIXED.get(parts.path)) is not None:
            return self.fulfill(route, fixed)
        if parts.path == "/probe-strip/":
            poll = self.strip.pop(0) if self.strip else ""
            return self.fulfill(route, _PROBE_STRIP.format(poll=poll))
        if parts.path == "/probe-dialog/":
            return self.fulfill(route, _dialog_probe())
        if parts.path == "/probe-asking-dialog/":
            return self.fulfill(route, self.asking)
        if parts.path == "/probe-panel/":
            return self.fulfill(route, self.panels.pop(0) if len(self.panels) > 1 else self.panels[0])
        if parts.path == "/probe-asking-head/":
            return self.fulfill(route, self.heads.pop(0) if len(self.heads) > 1 else self.heads[0])
        if parts.path == reverse("control_plane:visit"):
            # The read it asked for has already landed, unless a test says it is under way.
            return self.fulfill(route, self.visit, content_type="application/json")
        if parts.path == "/probe-visit-status/":
            return self.fulfill(route, _VISIT_DONE, content_type="application/json")
        if (address, part) in self.pages:
            return self.fulfill(route, self.pages[(address, part)])
        return super().respond(route)

    def reach(self, row, title):
        """Opens the rows, marks the page, and puts the keyboard on ``row``'s button."""

        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        self.button(row, title).focus()

    def press(self, row, title):
        self.reach(row, title)
        self.page.keyboard.press("Enter")

    def button(self, row, name):
        return self.page.get_by_role("listitem").filter(has_text=f"Example {row}").get_by_role("button", name=name)

    def rows_are(self, order):
        self.page.wait_for_function(f"(order) => ({_ROWS_SHOWN})() === order", arg=order)

    def test_a_press_answers_in_place_and_the_keyboard_stays_on_the_pressed_button(self):
        self.press("C", "Move up")
        self.rows_are("ACB")

        # No page was loaded: the page is the one the press was made on, and
        # what base.html draws in the content (the message) came with it.
        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertEqual(self.page.evaluate(_SAID), ["Example moved."])
        self.assertEqual(self.page.evaluate(_FOCUS), ["/probe-rows/C/", "up"])
        # What the plain post sends: the button's value, to the button's address.
        ((sent_to, body),) = self.posted
        self.assertEqual((sent_to, body["action"]), ("/probe-rows/C/", "up"))

    def test_a_row_moved_where_its_button_is_off_keeps_the_keyboard_in_the_row(self):
        self.press("B", "Move up")
        self.rows_are("BAC")

        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertEqual(self.page.evaluate(_FOCUS), ["/probe-rows/B/", "down"])

    def test_a_deleted_row_hands_the_keyboard_to_the_row_after_it(self):
        self.page.set_viewport_size({"width": 1360, "height": 600})
        self.reach("B", "Delete")
        at = self.page.evaluate("() => window.scrollY")
        self.page.keyboard.press("Enter")
        self.rows_are("AC")

        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertEqual(self.page.evaluate(_FOCUS), ["/probe-rows/C/", "up"])
        # The rows are reached by scrolling, and the page stays where it was.
        self.assertGreater(at, 0)
        self.assertEqual(self.page.evaluate("() => window.scrollY"), at)

    def test_a_press_in_a_region_of_its_own_answers_only_that_region(self):
        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        self.page.get_by_role("button", name="Mark").focus()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("() => document.querySelector('[data-probe=aside]').textContent === 'Marked'")

        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertEqual(
            self.page.evaluate("() => document.querySelector('[data-probe=other]').textContent"),
            "An example part beside the rows.",
        )

    def test_a_press_answered_with_no_page_shows_the_page_it_was_made_on(self):
        """What the press did is done, so an answer with nothing to place is
        read as the page; the content is never emptied in its place."""

        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        with self.page.expect_navigation(wait_until="load"):
            self.page.get_by_role("button", name="Quiet").click()

        self.assertFalse(self.page.evaluate("() => window.kept === true"))
        self.assertTrue(self.page.url.endswith("/probe-rows/"))
        self.assertEqual(self.page.evaluate(_ROWS_SHOWN), "ABC")

    def test_a_press_the_server_fails_keeps_the_page_and_says_so_among_its_messages(self):
        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        self.page.get_by_role("button", name="Broken").click()
        self.page.wait_for_function(f"() => ({_SAID})().length > 0")

        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertEqual(self.page.evaluate(_ROWS_SHOWN), "ABC")
        self.assertEqual(self.page.evaluate(_SAID), ["That could not be sent."])

    def test_a_press_refused_and_sent_on_to_another_page_says_why_where_it_was_pressed(self):
        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        self.page.get_by_role("button", name="Refused").click()
        self.page.wait_for_function(f"() => ({_SAID})().length > 0")

        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertTrue(self.page.url.endswith("/probe-rows/"))
        self.assertEqual(self.page.evaluate(_ROWS_SHOWN), "ABC")
        self.assertEqual(self.page.evaluate(_SAID), [_REFUSED])

    def test_a_press_refused_with_a_sentence_says_that_sentence(self):
        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        self.page.get_by_role("button", name="Declined").click()
        self.page.wait_for_function(f"() => ({_SAID})().length > 0")

        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        self.assertEqual(self.page.evaluate(_SAID), [_DECLINED])

    def test_the_content_answers_no_link_or_form_of_its_own(self):
        """The page's content is a region only for presses: its own links and
        forms lead where they lead, as they did."""

        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        for role, name in (("link", "Again"), ("button", "Send")):
            with self.subTest(name), self.page.expect_navigation(wait_until="load"):
                self.page.evaluate("() => { window.kept = true; }")
                self.page.get_by_role(role, name=name).click()
            self.assertFalse(self.page.evaluate("() => window.kept === true"))

    def test_without_script_a_press_posts_and_its_answer_is_the_page(self):
        self.start(java_script_enabled=False)
        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        with self.page.expect_navigation(wait_until="load"):
            self.button("C", "Move up").click()

        self.assertEqual(self.page.evaluate(_ROWS_SHOWN), "ACB")
        ((sent_to, body),) = self.posted
        self.assertEqual((sent_to, body["action"]), ("/probe-rows/C/", "up"))

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

    def test_a_dialogs_panel_asks_for_its_readings_once_shown_and_fills_in_place(self):
        visit = (reverse("control_plane:visit"), "")
        self.page.goto(f"{ORIGIN}/probe-asking-dialog/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        self.page.evaluate(SETTLE)
        # Shut, it is not being looked at: nothing is fetched or asked.
        self.assertNotIn(visit, self.asked)

        self.page.evaluate("() => document.querySelector('[data-probe=\"open\"]').click()")
        self.page.wait_for_function("() => document.querySelector('dialog').textContent.includes('as it is now')")

        self.assertEqual(self.asked.count(visit), 1)
        # Fetched from the panel's own address, not the page the dialog is on.
        self.assertEqual(self.asked.count(("/probe-panel/", "")), 2)
        self.assertTrue(self.page.evaluate("() => window.kept === true"))

    def test_a_pages_note_says_what_its_ask_waits_for_and_the_ask_outlives_the_swap(self):
        self.visit = _VISIT_LIVE
        self.page.goto(f"{ORIGIN}/probe-asking-head/", wait_until="load")
        note = "#content > [data-ask-note]"
        self.page.evaluate(
            "(note) => { document.querySelector('[data-ask]').kept = true; document.querySelector(note).was = true; }",
            note,
        )
        # Under way, the page's note says so: the note the form names.
        self.page.wait_for_function(
            "(note) => document.querySelector(note).textContent.includes('fresh reading')", arg=note
        )

        # Landed, everything but the ask is drawn again, the note with it,
        # and the new note is the one its form names.
        self.page.wait_for_function("() => document.querySelector('[data-probe=said]').textContent === 'as it is now'")
        state = self.page.evaluate(
            "(note) => ({kept: document.querySelector('[data-ask]').kept === true,"
            " newNote: document.querySelector(note).was !== true,"
            " named: [...document.querySelector('form[data-ask-auto]').elements].some((field) => field.matches(note))})",
            note,
        )
        self.assertEqual(state, {"kept": True, "newNote": True, "named": True})

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
