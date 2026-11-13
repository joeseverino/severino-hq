"""A form in a real browser: what a press sends while the form says it is
working, and what a long form's closing row does as it is edited."""

from typing import override
from urllib.parse import parse_qs

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.template import Context, Template
from django.test import Client

from hq.domains.projects.models import Project
from hq.platform.application.action_links import command_url
from hq.platform.application.capabilities import capability_words
from hq.platform.application.projects import DEPENDENCY_UPDATES
from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.probes.settle import PAINTED
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import NONCE, ORIGIN

# A page of HQ's sheets and app.js, after the fragment.js it builds on, with
# the form a post_button posts.
_PAGE = """{% load stylesheet_tags %}<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ title }}</title>
{% stylesheets %}<script defer src="{{ static }}js/fragment.js" nonce="{{ nonce }}"></script>
<script defer src="{{ static }}js/app.js" nonce="{{ nonce }}"></script></head>
<body><main class="content">{{ content }}</main>
<form id="hq-post" method="post" hidden></form></body></html>"""

# A settings form as an extension draws one: a table of rows in a section's
# frame, each with a select in a control cell and a row's own post_button,
# then the closing row pinned once the form changes, and a part after the frame.
_SETTINGS = (
    '{% section "Example settings" %}<form method="post" action="/example/settings/">'
    '{% table_region "Example accounts" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Account" "identifier" %}{% table_heading "Group" "control" %}'
    '{% table_heading "Remove" "control" hidden=True %}</tr></thead><tbody>'
    "{% for row in rows %}<tr><td>Example account {{ row }}</td>"
    '<td><select name="group-{{ row }}" aria-label="Group"><option>No example group</option>'
    "<option>Example</option></select></td>"
    '<td>{% post_button "Remove" "/example/remove/" %}</td></tr>{% endfor %}'
    "</tbody></table>{% endtable_region %}"
    '{% include "partials/_form_actions.html" with verb="Save changes" bar=True only %}'
    "</form>{% endsection %}<p data-after>An example line after the settings.</p>"
)

# One row's select in a narrow control cell, its longest choice chosen.
_CHOICE = (
    '{% section "Example choice" %}{% table_region "Example choice" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Account" "identifier" %}{% table_heading "Group" "control" %}</tr></thead><tbody>'
    '<tr><td>Example account with a name long enough to take the row</td><td><select aria-label="Group">'
    "<option>No example group</option><option>Example</option></select></td></tr></tbody></table>"
    "{% endtable_region %}{% endsection %}"
)


def _page(content, title="Example", **values):
    """``content``, through HQ's tags, alone in a page's main."""

    drawn = Template("{% load table_tags %}" + content).render(Context(values))
    return Template(_PAGE).render(
        Context({"static": settings.STATIC_URL, "nonce": NONCE, "content": drawn, "title": title})
    )


# Where each part below the closing row stands, and how tall the row is.
_BELOW = """([bar, frame]) => {
  const box = (selector) => document.querySelector(selector).getBoundingClientRect();
  return {bar: box(bar).height, frame: box(frame).bottom, after: box('[data-after]').top};
}"""

# What the closing row paints of its own where it stands: a band is a fill on
# the row, or one behind it in a color other than the frame's.
_BAND = """([row, frameSelector]) => {
  const bar = document.querySelector(row);
  const own = getComputedStyle(bar);
  const behind = getComputedStyle(bar, '::before');
  const frame = getComputedStyle(bar.closest(frameSelector)).backgroundColor;
  const filled = (color) => !/^(transparent|rgba\\(0, 0, 0, 0\\))$/.test(color);
  const bands = [];
  if (own.backgroundImage !== 'none' || filled(own.backgroundColor)) bands.push('the row is filled');
  if (behind.content !== 'none' && behind.visibility === 'visible'
      && (behind.backgroundImage !== 'none' || behind.backgroundColor !== frame)) bands.push('a band is painted behind the row');
  return bands;
}"""

# Whether the form the closing row ends is marked changed.
_MARKED = "(bar) => document.querySelector(bar).parentElement.hasAttribute('data-changed')"

# Whether the row's ground is drawn behind it.
_GROUND_SHOWN = """(bar) => {
  const behind = getComputedStyle(document.querySelector(bar), '::before');
  return behind.content !== 'none' && behind.visibility === 'visible';
}"""

# How much narrower a select is than its chosen words on one line.
_SHORT_OF_ITS_WORDS = """(choice) => {
  const select = document.querySelector(choice);
  const style = getComputedStyle(select);
  const context = document.createElement('canvas').getContext('2d');
  context.font = style.font;
  const words = context.measureText(select.selectedOptions[0].textContent).width;
  const room = select.clientWidth - parseFloat(style.paddingInlineStart) - parseFloat(style.paddingInlineEnd);
  return Math.max(0, words - room);
}"""


class CommandFormBrowserTests(BrowserGate):
    """A switch is pressed as one of two buttons: the one pressed is what posts."""

    scripted = True
    path: str

    @classmethod
    @override
    def render(cls):
        cls.path = command_url(DEPENDENCY_UPDATES)
        with transaction.atomic():
            Project.objects.create(
                name="Example",
                slug="example",
                repository_url="https://github.com/example/example",
                dependency_updates=True,
            )
            client = Client()
            client.force_login(get_user_model().objects.create_superuser(username="operator"))
            page = answered(client.get(command_url(DEPENDENCY_UPDATES, "example")))
            transaction.set_rollback(True)
        return {
            cls.path.strip("/"): page,
            # Where a command's post lands: a page of HQ's, which takes part
            # in the fade between pages as every page of HQ's does.
            "example/done": _page("Done", title="Done"),
            "example/settings": _page(_SETTINGS, rows=range(1, 25)),
            "example/choice": _page(_CHOICE),
        }

    @override
    def start(self, **options):
        super().start(**options)
        self.posted = []

    @override
    def respond(self, route):
        if route.request.method == "POST" and route.request.url.endswith("/example/remove/"):
            # A row's post_button answered with no page, so the one it was
            # pressed on stays to be read.
            self.posted.append(parse_qs(route.request.post_data or ""))
            return route.fulfill(status=204)
        if route.request.method == "POST":
            self.posted.append(parse_qs(route.request.post_data or ""))
            return self.fulfill(route, self.pages["example/done"])
        return super().respond(route)

    def test_the_pressed_switch_posts_its_value_though_the_form_disables_it(self):
        self.page.goto(f"{ORIGIN}{command_url(DEPENDENCY_UPDATES, 'example')}", wait_until="load")

        self.page.get_by_role("button", name=capability_words(DEPENDENCY_UPDATES, "off")).click()
        self.page.wait_for_function("() => document.title === 'Done'")

        (posted,) = self.posted
        self.assertEqual(posted.get("dependency_updates"), ["false"])
        self.assertEqual(posted.get("__target"), ["example"])

    def open_settings(self):
        self.page.set_viewport_size({"width": 1360, "height": 900})
        self.page.goto(f"{ORIGIN}/example/settings/", wait_until="load")

    def test_an_edit_pins_the_closing_row_and_moves_nothing_below_it(self):
        self.open_settings()
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        before = self.page.evaluate(_BELOW, [SELECTORS["save_bar"], SELECTORS["section_frame"]])

        self.page.locator(SELECTORS["choice"]).last.select_option("Example")

        self.assertTrue(self.page.evaluate(_MARKED, SELECTORS["save_bar"]))
        self.assertEqual(self.page.evaluate(_BELOW, [SELECTORS["save_bar"], SELECTORS["section_frame"]]), before)
        self.assertEqual(
            self.page.evaluate(_BAND, [SELECTORS["save_bar"], SELECTORS["section_frame"]]),
            [],
            "the row paints a band in its place",
        )

    def test_the_closing_row_covers_what_scrolls_beneath_it_only_while_stuck(self):
        self.open_settings()
        if not self.page.evaluate("CSS.supports('container-type: scroll-state')"):
            self.skipTest("this engine cannot tell a stuck row from one in its place")
        self.page.locator(SELECTORS["choice"]).first.select_option("Example")
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.evaluate(PAINTED)

        self.assertTrue(
            self.page.evaluate(_GROUND_SHOWN, SELECTORS["save_bar"]),
            "a stuck row leaves what scrolls beneath it showing",
        )
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.page.evaluate(PAINTED)
        self.assertFalse(
            self.page.evaluate(_GROUND_SHOWN, SELECTORS["save_bar"]), "a row in its place paints its ground"
        )

    def test_a_rows_post_button_posts_its_own_form_and_marks_nothing(self):
        self.open_settings()

        with self.page.expect_response(lambda response: response.request.method == "POST"):
            self.page.get_by_role("button", name="Remove").first.click(no_wait_after=True)

        self.assertEqual(len(self.posted), 1)
        self.assertFalse(self.page.evaluate(_MARKED, SELECTORS["save_bar"]))

    def test_a_select_in_a_narrow_control_cell_keeps_its_chosen_words_on_one_line(self):
        if self.browser.browser_type.name == "webkit":
            self.skipTest("WebKit draws its own select, which the stylesheet does not reach")
        self.page.set_viewport_size({"width": 820, "height": 900})
        self.page.goto(f"{ORIGIN}/example/choice/", wait_until="load")

        self.assertEqual(self.page.evaluate(_SHORT_OF_ITS_WORDS, SELECTORS["choice"]), 0)
