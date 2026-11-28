"""Fragments in a real browser: a region fetched, refreshed and drawn again as fragment.js answers it."""

import re
import threading
from datetime import UTC, datetime
from typing import override
from urllib.parse import parse_qsl, urlsplit

from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

from hq.platform.application.asks import DONE, QUEUED, Standing
from hq.platform.application.pages import PageAction, PageVisit
from hq.platform.application.visit_refresh import FOLLOWS
from hq.platform.core import ahead
from hq.platform.core.models import UpstreamReading
from hq.platform.core.tests.browser.gate import BrowserGate, answered, sent
from hq.platform.core.tests.browser.probes.settle import SETTLE
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import NONCE, ORIGIN

# fragment.js and ask.js, each through the part that draws the frames it
# clones, as base.html loads them.
_FRAGMENT_JS = render_to_string("scripts/fragment.html", {"csp_nonce": NONCE})
_ASK_JS = render_to_string("scripts/ask.html", {"csp_nonce": NONCE})

# A live region that names its parts, a disclosure and a menu, a
# placeholder behind a closed disclosure, and one whose address answers a
# whole page. The page hears what commits from the change it was drawn after
# (``_STREAM``, filled in as it is answered).
_FRAGMENT_PROBE = f"""<!doctype html><html><head><title>Probe</title>
{_FRAGMENT_JS}
</head><body data-live="{{_STREAM}}"><main>
<section data-fragment="/probe-strip/" data-fragment-live="estate">
<details data-fragment-part="first"><summary>First</summary>as drawn</details>
<div data-fragment-part="second" data-menu><button popovertarget="second-panel">Second</button><div id="second-panel" popover>as drawn</div></div>
</section>
<details data-probe="fold"><summary>More</summary>
<div data-fragment="/probe-slot/" data-fragment-load data-fragment-failure="Could not be read.">waiting</div>
</details>
<div data-fragment="/probe-page/" data-fragment-load data-fragment-failure="Could not be read.">waiting</div>
</main></body></html>"""

_PROBE_STRIP = """<section data-fragment="/probe-strip/">
<details data-fragment-part="first"><summary>First</summary>as drawn</details>
<div data-fragment-part="second" data-menu><button popovertarget="second-panel">Second</button><div id="second-panel" popover>read again</div></div>
</section>"""

# A form answered in place whose submitting button shares a field's name,
# between that field and another.
_FORM_PROBE = f"""<!doctype html><html><head><title>Probe</title>
{_FRAGMENT_JS}
</head><body><main>
<form action="/probe-sent/" data-fragment-target="sent">
<input name="key" value="field">
<button name="key" value="button">Send</button>
<input name="note" value="last">
</form>
<div id="sent" data-fragment="/probe-sent/">waiting</div>
</main></body></html>"""

# One write form per kind of answer, each drawn with the tag every write form
# carries its retry key from, answered in place.
_KEYED_ANSWERS = ("ok", "refused", "broken", "lost")


def _keyed_probe():
    form = (
        '{% load write_tags %}<form method="post" action="/probe-keyed/{answer}/" data-fragment-target="sent">'
        '{% write_token %}<button data-probe="{answer}">Send</button></form>'
    )
    forms = "".join(Template(form.replace("{answer}", answer)).render(Context({})) for answer in _KEYED_ANSWERS)
    return f"""<!doctype html><html><head><title>Probe</title>
{_FRAGMENT_JS}
</head><body><main>
{forms}
<div id="sent" data-fragment="/probe-sent/">waiting</div>
</main></body></html>"""


# A region that asks to add its answers to history, which nothing restores.
_PUSH_PROBE = f"""<!doctype html><html><head><title>Probe</title>
{_FRAGMENT_JS}
</head><body><main>
<div id="pushed" data-fragment data-fragment-links data-fragment-history="push">
<a data-probe="push" href="/probe-push/?turn=2">Next</a>
</div>
</main></body></html>"""


def _stored():
    """A reading stored and taken back, each committed, and the keeper's pass
    that finds them, from a thread of its own: Django refuses this one the
    database while Playwright's loop runs."""

    def store():
        try:
            keeper = ahead.Keeper()
            keeper.turn()
            UpstreamReading.objects.create(key="example.live", value={}, observed_at=datetime.now(UTC))
            UpstreamReading.objects.filter(key="example.live").delete()
            keeper.turn()
        finally:
            connections.close_all()

    writer = threading.Thread(target=store)
    writer.start()
    writer.join()


# The probes whose answer never changes, by their address.
_FIXED = {
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
{_FRAGMENT_JS}
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
{_FRAGMENT_JS}
{_ASK_JS}
</head><body data-live="/live/"><main>
<button type="button" data-probe="open" command="show-modal" commandfor="modal-probe">Open</button>
{dialog}
</main></body></html>""",
        _PANEL.format(ask=ask, said="as last read"),
        _PANEL.format(ask="", said="as it is now"),
    )


# A page about one thing: the content draws its ask, and the page's own head
# its note, first as last read and then as it is now.
_ASKING_HEAD = """<main class="content" id="content">
{head}
<p data-probe="said">{said}</p>
{control}
</main>"""


def _asking_head_probe():
    visit = PageVisit("page", "example")
    head = render_to_string("partials/_page_head.html", {"page": {"title": "Probe"}, "page_visit": visit})
    control = Template('{% include "partials/_visit_refresh.html" with visit=visit %}').render(
        Context({"visit": visit})
    )
    scripts = f"""<!doctype html><html><head><title>Probe</title>
{_FRAGMENT_JS}
{_ASK_JS}
</head><body data-live="/live/">"""
    return [
        scripts + _ASKING_HEAD.format(head=head, said=said, control=control) + "</body></html>"
        for said in ("as last read", "as it is now")
    ]


def _visit_control(standing, **named):
    """The control a visit's view answers, its readings standing as ``standing``."""

    return render_to_string(
        "partials/_visit_refresh.html", {**named, "visit_standing": standing, "visit_follows": FOLLOWS}
    )


# The readings a page asked for, under way, then landed.
_VISIT_LIVE = Standing(QUEUED, "Waiting for a fresh reading.")
_VISIT_DONE = Standing(DONE, "Refreshed just now.")


_PROBE_STATE = """(failure) => ({
  first: document.querySelector('[data-fragment-part="first"]').kept === true,
  firstOpen: document.querySelector('[data-fragment-part="first"]').open,
  second: document.querySelector('[data-fragment-part="second"]').textContent,
  secondOpen: document.querySelector('[data-fragment-part="second"] [popover]').matches(':popover-open'),
  live: document.querySelector('[data-fragment-live]') !== null,
  slot: document.querySelector('[data-probe="fold"]').textContent,
  page: [...document.querySelectorAll(failure)].map((line) => line.textContent),
  titles: document.querySelectorAll('main title, main main').length,
})"""


# Rows a page lets the reader reorder and delete, each by its own
# posting button, drawn in a real page's frame: base.html's content, its one post
# form and its scripts. Between enough lines that the rows are reached by
# scrolling; beside them a region of its own with a press of its own, a press
# answered with no page and one the server fails, and a link and a form of
# the page's own.
_ROWS = """{% load action_tags notice_tags %}{% if said %}<ul class="messages"><li class="msg">{{ said }}</li></ul>{% endif %}
{% if held %}<ul class="messages"><li>{% notice tone="error" %}{{ held }}{% endnotice %}</li></ul>{% endif %}
<p data-probe="other">{{ other }}</p>
{% for line in lines %}<p>An example line {{ line }}.</p>{% endfor %}
<ul class="list-rows" data-probe="rows">{% for row in rows %}<li data-row="{{ row.key }}"><span>Example {{ row.key }}</span>
{% button "\u2191" offer=row.up look="glyph" title="Move up" disabled=forloop.first %}
{% button "\u2193" offer=row.down look="glyph" title="Move down" disabled=forloop.last %}
{% button "\u00d7" offer=row.delete look="glyph" title="Delete" %}
</li>{% endfor %}</ul>
<div id="probe-aside" data-fragment><span data-probe="aside">{{ aside }}</span>
{% button "Mark" offer=presses.mark %}</div>
{% button "Quiet" offer=presses.quiet %}
{% button "Broken" offer=presses.broken %}
{% button "Refused" offer=presses.refused %}
{% button "Declined" offer=presses.declined %}
<table><tbody><tr id="probe-card"><td>Example card</td><td>{% button "Held" offer=presses.held %}</td></tr></tbody></table>
<a data-probe="link" href="/probe-rows/?again=1">Again</a>
<form method="post" action="/probe-rows/plain/"><button data-probe="plain">Send</button></form>
{% for line in lines %}<p>An example line after {{ line }}.</p>{% endfor %}"""
_MAIN = re.compile(r"(<main\b[^>]*>).*?(</main>)", re.DOTALL)

# Why the server refused a press: drawn among the messages of the page it
# sent the press on to, and as the whole answer to one it answered itself.
_REFUSED = "Example refused: its connection only reads."
_DECLINED = "Example declined: it needs a connection that manages."
# Refused, and sent back to the page it was pressed on, which says why among its messages.
_HELD = "Example held: another run holds it."
# Another page, saying the refusal among its messages. Read again, it would
# not: a message is said once.
_ELSEWHERE = """<!doctype html><html><head><title>Elsewhere</title></head><body>
<main class="content" id="elsewhere"><ul class="messages"><li>{% notice tone="error" %}{{ refused }}{% endnotice %}</li></ul></main>
</body></html>"""

# What each press leaves of the rows.
_PRESSED = {("ABC", "C", "up"): "ACB", ("ABC", "B", "up"): "BAC", ("ABC", "B", "delete"): "AC"}

# The rows in the order the page shows them, and the page's messages: the
# list base.html draws first in the content.
_ROWS_SHOWN = "() => [...document.querySelectorAll('[data-row]')].map((row) => row.dataset.row).join('')"
_SAID = "() => [...document.querySelectorAll('main > ul:first-child > li')].map((line) => line.textContent.trim())"

# Where the keyboard is: which posting button, by where it posts and what it says.
_FOCUS = "() => [document.activeElement.getAttribute('formaction'), document.activeElement.value]"
_REACHED = "(b) => { b.scrollIntoView({block: 'center', behavior: 'instant'}); b.focus({preventScroll: true}); return scrollY; }"


def _posted(request):
    """A post's fields, sent by a form (urlencoded) or by a script (multipart)."""

    body = request.post_data or ""
    if "multipart/form-data" in (request.headers.get("content-type") or ""):
        return dict(re.findall(r'name="([^"]+)"\r\n\r\n([^\r]*)', body))
    return dict(parse_qsl(body))


def offer_of(url, value=""):
    """A press as an offer carries it, for a page drawn without HQ's commands."""

    return PageAction._built(label=value, url=url, method="post", sends={"action": value})


def _row(key):
    url = f"/probe-rows/{key}/"
    return {"key": key, **{way: offer_of(url, way) for way in ("up", "down", "delete")}}


def _rows_pages(frame):
    """The rows page in each state a press leaves it, in ``frame``'s page."""

    # Each press of the page's own, by what it sends, at where it posts.
    where = {
        "mark": "aside",
        "quiet": "quiet",
        "broken": "broken",
        "refused": "refused",
        "declined": "declined",
        "held": "held",
    }
    presses = {sent: offer_of(f"/probe-rows/{path}/", sent) for sent, path in where.items()}

    def draw(order, **values):
        content = Template(_ROWS).render(
            Context(
                {
                    "rows": [_row(key) for key in order],
                    "presses": presses,
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
    pages["held"] = draw("ABC", held=_HELD)
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
        # The page's stream, held open until a test answers it.
        self.streams = []
        self.drawn = ""
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
                return self.fulfill(route, Template(_ELSEWHERE).render(Context({"refused": _REFUSED})))
            if row == "declined":
                # Refused, answered with the sentence alone (``refusal_response``).
                return route.fulfill(status=409, content_type="text/plain; charset=utf-8", body=_DECLINED)
            if row == "quiet":
                # What a view gives a script that asks with nothing to show.
                return route.fulfill(status=204)
            if row == "held":
                return self.fulfill(route, self.rows["held"])
            if row == "aside":
                self.order = "marked"
            elif row != "plain":
                self.order = _PRESSED[(self.order, row, fields["action"])]
        return self.fulfill(route, self.rows[self.order])

    def answer_keyed(self, route, answer):
        """What a write answers: HQ's answer, a refusal, a failure of the
        server's own, or none at all."""

        self.posted.append((answer, _posted(route.request)))
        # The first sending of a write gets no answer; its second, HQ's.
        if answer == "lost" and sum(each == "lost" for each, _ in self.posted) == 1:
            return route.abort()
        if answer == "broken":
            return route.fulfill(status=500, body="Example failure")
        if answer == "refused":
            return route.fulfill(status=400, content_type="text/plain; charset=utf-8", body="Example refused.")
        return self.fulfill(route, _FIXED["/probe-sent/"])

    @override
    def stream(self, route):
        self.streams.append(route)

    @override
    def respond(self, route):
        request = route.request
        parts = urlsplit(request.url)
        address = parts.path + (f"?{parts.query}" if parts.query else "")
        part = request.headers.get("x-fragment", "")
        self.asked.append((address, part))
        if parts.path.startswith("/probe-rows/"):
            return self.answer_rows(route, parts)
        if parts.path == "/probe-keyed-form/":
            return self.fulfill(route, _keyed_probe())
        if parts.path.startswith("/probe-keyed/"):
            return self.answer_keyed(route, parts.path.removeprefix("/probe-keyed/").strip("/"))
        if parts.path == "/probe/":
            return self.fulfill(route, _FRAGMENT_PROBE.replace("{_STREAM}", f"/live/?after={self.drawn}"))
        if (fixed := _FIXED.get(parts.path)) is not None:
            return self.fulfill(route, fixed)
        if parts.path == "/probe-strip/":
            return self.fulfill(route, _PROBE_STRIP)
        if parts.path == "/probe-dialog/":
            return self.fulfill(route, _dialog_probe())
        if parts.path == "/probe-asking-dialog/":
            return self.fulfill(route, self.asking)
        if parts.path == "/probe-panel/":
            return self.fulfill(route, self.panels.pop(0) if len(self.panels) > 1 else self.panels[0])
        if parts.path == "/probe-asking-head/":
            return self.fulfill(route, self.heads.pop(0) if len(self.heads) > 1 else self.heads[0])
        if parts.path == reverse("control_plane:visit"):
            # The control, as the readings it asked for stand: landed, unless a test says under way.
            posted = re.findall(r'name="(\w+)"\r\n\r\n([^\r]*)', request.post_data or "")
            form = dict(posted) if request.method == "POST" else dict(parse_qsl(parts.query))
            named = {name: form.get(name, "") for name in ("subject", "name", "refresh")}
            return self.fulfill(route, _visit_control(self.visit, **named))
        if (address, part) in self.pages:
            return self.fulfill(route, self.pages[(address, part)])
        return super().respond(route)

    def reach(self, row, title):
        """Opens the rows, marks the page, and puts the keyboard on ``row``'s button scrolled
        into view, as whether focus() scrolls is each engine's own; answers where the page is."""

        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        return self.button(row, title).evaluate(_REACHED)

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
        at = self.reach("B", "Delete")
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

    def test_a_press_refused_on_a_card_says_why_on_that_card_and_keeps_what_was_opened(self):
        self.page.goto(f"{ORIGIN}/probe-rows/", wait_until="load")
        # The reader opened the card's fold (tables.js) before pressing.
        self.page.evaluate(
            "() => { window.kept = true; document.getElementById('probe-card').toggleAttribute('data-open', true); }"
        )
        self.page.get_by_role("button", name="Held").click()
        self.page.wait_for_function("() => document.querySelector('#probe-card [role=alert]')")

        self.assertTrue(self.page.evaluate("() => window.kept === true"))
        said = "() => [...document.querySelectorAll('#probe-card [role=alert]')].map((line) => line.textContent.trim())"
        self.assertEqual(self.page.evaluate(said), [_HELD])
        # Said once, on the card, and not at the top of the page.
        self.assertEqual(self.page.evaluate(_SAID), [])
        self.assertTrue(self.page.evaluate("() => document.getElementById('probe-card').hasAttribute('data-open')"))

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

    def test_a_live_region_is_drawn_again_when_a_change_to_it_commits(self):
        from hq.platform.core import live

        self.drawn = live.now()
        with self.page.expect_request(lambda request: urlsplit(request.url).path == "/live/"):
            self.page.goto(f"{ORIGIN}/probe/", wait_until="load")
        self.page.evaluate(
            "() => { const first = document.querySelector('[data-fragment-part=\"first\"]');"
            " first.kept = true; first.open = true;"
            " document.querySelector('[data-fragment-part=\"second\"] [popovertarget]').click(); }"
        )
        (stream,) = self.streams
        heard = dict(parse_qsl(urlsplit(stream.request.url).query))["after"]
        _stored()
        stream.fulfill(headers={"Content-Type": "text/event-stream"}, body=live.said(*live.HUB.since(heard)))
        self.page.wait_for_function(
            "() => document.querySelector('[data-fragment-part=\"second\"]').textContent.includes('read again')"
        )
        state = self.page.evaluate(_PROBE_STATE, self.css(SELECTORS["fragment_failure"]))

        # The part that did not change is the same node, still open; the one
        # that did was replaced, its menu open as the reader left it. The
        # answer no longer draws it live, so it is no longer.
        self.assertTrue(state["first"])
        self.assertTrue(state["firstOpen"])
        self.assertEqual(state["second"].strip(), "Secondread again")
        self.assertTrue(state["secondOpen"])
        self.assertFalse(state["live"])

    def test_a_placeholder_waits_for_its_disclosure_and_refuses_a_whole_page(self):
        self.page.goto(f"{ORIGIN}/probe/", wait_until="load")
        self.page.wait_for_selector(self.css(SELECTORS["fragment_failure"]))
        state = self.page.evaluate(_PROBE_STATE, self.css(SELECTORS["fragment_failure"]))

        self.assertNotIn(("/probe-slot/", ""), self.asked)
        self.assertEqual(state["page"], ["Could not be read."])
        self.assertEqual(state["titles"], 0)

        self.page.locator(self.css(SELECTORS["fragment_fold"])).click()
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

    def landed(self):
        """The readings asked for land, and the page's stream says so."""

        self.page.wait_for_function("() => document.querySelector('[data-fragment-live]')")
        if not self.streams:
            with self.page.expect_request(lambda request: urlsplit(request.url).path == "/live/"):
                pass
        self.visit = _VISIT_DONE
        (stream,) = self.streams
        stream.fulfill(headers={"Content-Type": "text/event-stream"}, body="id: 1\ndata: estate\n\n")

    def test_a_dialogs_panel_asks_for_its_readings_once_shown_and_fills_in_place(self):
        visit = (reverse("control_plane:visit"), "")
        self.visit = _VISIT_LIVE
        self.page.goto(f"{ORIGIN}/probe-asking-dialog/", wait_until="load")
        self.page.evaluate("() => { window.kept = true; }")
        self.page.evaluate(SETTLE)
        # Shut, it is not being looked at: nothing is fetched or asked.
        self.assertNotIn(visit, self.asked)

        self.page.evaluate("() => document.querySelector('[data-probe=\"open\"]').click()")
        self.landed()
        self.page.wait_for_function("() => document.querySelector('dialog').textContent.includes('as it is now')")

        self.assertEqual(self.asked.count(visit), 1)
        # Fetched from the panel's own address, not the page the dialog is on.
        self.assertEqual(self.asked.count(("/probe-panel/", "")), 2)
        self.assertTrue(self.page.evaluate("() => window.kept === true"))

    def test_a_pages_ask_outlives_the_swap(self):
        self.visit = _VISIT_LIVE
        self.page.goto(f"{ORIGIN}/probe-asking-head/", wait_until="load")
        note = self.css(SELECTORS["page_note"])
        self.page.evaluate("(note) => { document.querySelector(note).was = true; }", note)
        self.landed()
        # Landed, everything but the ask is drawn again, the note with it,
        # and the new note is the one its form names.
        self.page.wait_for_function("() => document.querySelector('[data-probe=said]').textContent === 'as it is now'")
        state = self.page.evaluate(
            "(note) => ({asks: document.querySelectorAll('[data-ask]').length,"
            " newNote: document.querySelector(note).was !== true,"
            " named: [...document.querySelector('form[data-ask-auto]').elements].some((field) => field.matches(note))})",
            note,
        )
        self.assertEqual(state, {"asks": 1, "newNote": True, "named": True})

    def test_paging_the_calendar_asks_for_its_part_and_keeps_the_page(self):
        self.page.goto(f"{ORIGIN}{self.calendar}", wait_until="load")
        was = self.page.locator(self.css(SELECTORS["calendar_period"])).text_content()
        self.page.evaluate("() => { window.kept = true; }")
        self.page.locator(self.css(SELECTORS["calendar_next"])).focus()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function(
            "([period, was]) => document.querySelector(period).textContent !== was",
            arg=self.css([SELECTORS["calendar_period"], was]),
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

    def test_a_write_form_takes_a_new_key_once_hq_has_answered_and_keeps_it_when_none_did(self):
        """A write no answer came back for is sent again with its key, so
        HQ acts on it once, until HQ answers, and is never said to have failed."""

        for answer, spent in (("ok", True), ("refused", True), ("broken", False), ("lost", True)):
            with self.subTest(answer):
                self.start()
                self.page.goto(f"{ORIGIN}/probe-keyed-form/", wait_until="load")
                key = f'form[action="/probe-keyed/{answer}/"] input[name="__retry_key"]'
                before = self.page.locator(key).input_value()
                self.page.locator(f'[data-probe="{answer}"]').click()
                if spent:
                    self.page.wait_for_function(
                        "([key, was]) => document.querySelector(key).value !== was", arg=[key, before]
                    )
                # Over once the server has seen it and the page has settled.
                self.page.wait_for_function("() => document.readyState === 'complete'")
                self.page.evaluate(SETTLE)
                after = self.page.locator(key).input_value()

                sent = [fields["__retry_key"] for each, fields in self.posted if each == answer]
                self.assertEqual(sent, [before] * (2 if answer == "lost" else 1))
                if spent:
                    self.assertRegex(after, r"^web:[0-9a-f]{48}$")
                else:
                    self.assertEqual(after, before)
                if answer == "lost":
                    # Chromium logs the post the route aborted as a failed
                    # load: the test's own doing, as a failed status is
                    # (gate's _FAILED_ANSWER).
                    self.refusals = [heard for heard in self.refusals if "Failed to load resource: net::" not in heard]
