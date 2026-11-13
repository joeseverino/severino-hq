"""A dense page's map, as a reader moves through it: the link the keyboard is
on, and the link of the section being read, are in the row's view, and
bringing one there never moves the page, and a map an answer brings in is
marked as the one it replaced was."""

from typing import override

from django.conf import settings
from django.template import Context, Template

from hq.platform.core.tests.browser.catalog import CATALOG, part_page
from hq.platform.core.tests.browser.gate import BrowserGate, tab_key, with_script
from hq.platform.core.tests.browser.probes.settle import PAINTED
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import NONCE, ORIGIN
from hq.platform.core.tests.browser.widths import PHONE_SCREEN

_MAP = "part/page-navigation"
# app.js after fragment.js, as base.html loads them.
_SCRIPT = (
    '<script defer src="{{ static }}js/fragment.js" nonce="{{ nonce }}"></script>'
    '<script defer src="{{ static }}js/app.js" nonce="{{ nonce }}"></script>'
)

# The catalog's map over sections a screen tall each, so the last ones are
# reached only by scrolling the page, as on a dense page.
_DENSE = (
    '{% include "partials/_page_head.html" %}{% include "partials/_page_navigation.html" with '
    "navigation=page.navigation %}{% for item in page.navigation.items %}"
    "{% section item.label id=item.id %}{% for line in lines %}<p>An example line.</p>{% endfor %}"
    "{% endsection %}{% endfor %}"
)

# The same, inside a region answered from its own page, as a search's
# results hold their groups' map.
_REGION = "example-results"
_SWAPPED = f'<div id="{_REGION}" data-fragment>{_DENSE}</div>'

# The region answered again, and then the text of each link marked as the
# section being read.
_ANSWERED = f"() => hqFragment.swap(document.getElementById('{_REGION}'))"
_MARKED = """(current) => [...document.querySelectorAll(current)].map((link) => link.textContent.trim())"""

# A link (the focused one, without a selector), how far it stands outside the
# row's visible box on either side (0 inside it), and where the page is.
_SEEN = """([list, link]) => {
  const row = document.querySelector(list);
  const at = (link ? document.querySelector(link) : document.activeElement).getBoundingClientRect();
  const box = row.getBoundingClientRect();
  return {
    link: (link ? document.querySelector(link) : document.activeElement).textContent.trim(),
    outside: [Math.max(0, Math.round(box.left - at.left)), Math.max(0, Math.round(at.right - box.right))],
    page: window.scrollY,
    overflows: row.scrollWidth > row.clientWidth,
  };
}"""

# The page moved to a section's top, at once, as a reader's jump puts it.
_SCROLL_TO = "(id) => { window.scrollTo({top: document.getElementById(id).offsetTop, behavior: 'instant'}); return window.scrollY; }"


def _page(markup=_DENSE):
    drawn = Template(markup).render(Context({**CATALOG[_MAP].context, "lines": range(40)}))
    script = Template(_SCRIPT).render(Context({"static": settings.STATIC_URL, "nonce": NONCE}))
    return part_page(_MAP, drawn).replace("</head>", script + "</head>")


class PageNavigationBrowserTests(BrowserGate):
    """The map of the catalog's dense page, on a phone, where its row of links
    is wider than the screen and scrolls sideways."""

    @classmethod
    @override
    def render(cls):
        return {"page-navigation": _page(), "page-navigation-answered": _page(_SWAPPED)}

    def open_map(self, address="", page="page-navigation"):
        self.page.set_viewport_size({"width": PHONE_SCREEN[0], "height": PHONE_SCREEN[1]})
        self.page.goto(f"{ORIGIN}/{page}/{address}", wait_until="load")
        self.page.evaluate(PAINTED)

    def seen(self, link=None):
        seen = self.page.evaluate(_SEEN, [SELECTORS["page_nav_list"], link])
        self.assertTrue(seen["overflows"], "the map fits its row, so nothing here is past its edge")
        return seen

    @with_script
    def test_each_link_tabbed_to_is_inside_the_rows_view(self):
        """Pressing Tab through the map brings each link it lands on inside
        the row's visible box, a link partly past the edge too, and the page
        stays where it was."""

        self.open_map()
        links = [" ".join(text.split()) for text in self.page.locator(SELECTORS["page_nav_link"]).all_inner_texts()]
        reached = []
        for _ in links:
            self.page.keyboard.press(tab_key(self.browser))
            self.page.evaluate(PAINTED)
            seen = self.seen()
            reached.append(" ".join(seen["link"].split()))
            with self.subTest(link=reached[-1]):
                self.assertEqual(seen["outside"], [0, 0], seen)
                self.assertEqual(seen["page"], 0, seen)
        self.assertEqual(reached, links)

    @with_script
    def test_the_section_being_read_is_marked_inside_the_rows_view(self):
        """Opened at the last section, the map marks its link as the section
        being read, and that link is inside the row's visible box."""

        self.open_map("#example-5")
        current = self.seen(SELECTORS["page_nav_current"])
        self.assertEqual(current["link"], "Example history", current)
        self.assertEqual(current["outside"], [0, 0], current)

    @with_script
    def test_a_scroll_that_changes_the_section_being_read_never_moves_the_page(self):
        """As the page is scrolled to its last section and back, the map
        brings the new section's link into its row at once, with motion or
        without, and the page stays exactly where the reader put it."""

        for motion in ("no-preference", "reduce"):
            with self.subTest(motion=motion):
                self.page.emulate_media(reduced_motion=motion)
                self.open_map()
                for section, label in (("example-5", "Example history"), ("example-0", "Overview")):
                    put = self.page.evaluate(_SCROLL_TO, section)
                    self.page.evaluate(PAINTED)
                    current = self.seen(SELECTORS["page_nav_current"])
                    self.assertEqual(current["link"], label, current)
                    self.assertEqual(current["outside"], [0, 0], current)
                    self.assertEqual(current["page"], put, current)

    @with_script
    def test_a_map_an_answer_brings_in_marks_the_section_being_read(self):
        """Its region answered again in place, the map that replaced the one
        drawn marks the section being read, and goes on marking it as the
        page is scrolled."""

        self.open_map("#example-5", page="page-navigation-answered")
        self.assertEqual(self.page.evaluate(_MARKED, SELECTORS["page_nav_current"]), ["Example history"])
        self.assertTrue(self.page.evaluate(_ANSWERED), "the region's answer was not placed")
        self.page.evaluate(PAINTED)
        self.assertEqual(self.page.evaluate(_MARKED, SELECTORS["page_nav_current"]), ["Example history"])
        self.page.evaluate(_SCROLL_TO, "example-0")
        self.page.evaluate(PAINTED)
        self.assertEqual(self.page.evaluate(_MARKED, SELECTORS["page_nav_current"]), ["Overview"])
