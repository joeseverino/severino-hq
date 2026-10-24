"""The API reference: a vendored viewer, held to the rules its frame and palette must keep."""

import json
import re

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client
from django.urls import reverse

from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.probes.palette import _CONTRAST, LARGE_TEXT_CONTRAST, TEXT_CONTRAST
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE


def _opaque(color: str) -> tuple[int, ...]:
    """A computed color's red, green and blue, without its opacity, as 0-255:
    ``rgb(255, 255, 255)`` and ``color(srgb 1 1 1 / 0.8)`` are one color."""

    numbers = [float(n) for n in re.findall(r"[\d.]+", color.split("/")[0])]
    if color.startswith("color("):
        return tuple(round(n * 255) for n in numbers[:3])
    return tuple(round(n) for n in numbers[:3])


# The API reference: a vendored viewer mounted inside HQ's frame. Its markup is
# the vendor's, so the probes below find it by tag and ARIA, never by class.
REFERENCE = "api/docs"

REFERENCE_DARK = "api/docs/dark"

# Where the viewer keeps its sidebar open beside the content.
REFERENCE_DESKTOP = (1024, WIDE)

REFERENCE_PHONE = PHONE_SCREEN[0]

# The viewer is one large script: parsing it takes longer than a page's load.
REFERENCE_READY = 30_000

_REFERENCE_MOUNTED = "() => document.querySelector('main main h1') !== null"


def wait_for_reference(page):
    """Until the viewer has drawn the reference."""

    page.wait_for_function(_REFERENCE_MOUNTED, timeout=REFERENCE_READY)


# The sidebar is a list of entries, each named by its own link. An entry that
# opens to show more carries the control that opens it; an operation is a leaf.
_REFERENCE_ENTRY = """(path) => {
  let entry = document.querySelector('aside');
  for (const title of path) {
    entry = [...entry.querySelectorAll(':scope > ul > li')].find((item) =>
      item.querySelector(':scope > a, :scope > div > a').textContent.trim().startsWith(title));
    if (!entry) return null;
  }
  return entry.querySelector(':scope > a, :scope > div > a');
}"""

# The sidebar as drawn: each top-level entry, and the entries under it that open in turn.
_REFERENCE_OUTLINE = """() => {
  const link = (item) => item.querySelector(':scope > a, :scope > div > a');
  return [...document.querySelectorAll('aside > ul > li')].map((item) => [
    link(item).getAttribute('href'), link(item).textContent.trim(),
    [...item.querySelectorAll(':scope > ul > li')]
      .filter((child) => child.querySelector(':scope > div > [aria-expanded]'))
      .map((child) => link(child).textContent.trim()),
  ]);
}"""

# Every top-level group opened, as a reader opens it.
_REFERENCE_OPEN_GROUPS = """() => document
  .querySelectorAll('aside > ul > li > div > [aria-expanded="false"]').forEach((control) => control.click())"""

_REFERENCE_GROUPS_OPEN = """() => [...document.querySelectorAll('aside > ul > li > div > [aria-expanded]')]
  .every((control) => control.getAttribute('aria-expanded') === 'true'
    && control.closest('li').querySelector(':scope > ul') !== null)"""

# What stands over HQ's header, what of the viewer's is pinned under it, and
# any trail the viewer is showing.
_REFERENCE_FRAME = """(root) => {
  const header = document.querySelector('header');
  const bar = header.getBoundingClientRect();
  const over = [0.05, 0.25, 0.5, 0.75, 0.95]
    .map((share) => document.elementFromPoint(bar.left + bar.width * share, bar.top + bar.height / 2))
    .filter((el) => !header.contains(el))
    .map((el) => el.tagName.toLowerCase());
  const pinned = [...document.querySelector(root).querySelectorAll('aside, nav, header, [data-scalar-scroll-header]')]
    .filter((el) => el.checkVisibility() && ['sticky', 'fixed'].includes(getComputedStyle(el).position))
    .map((el) => [el.tagName.toLowerCase(), Math.round(el.getBoundingClientRect().top)])
    .filter(([, top]) => top < Math.round(bar.bottom) - 1);
  const trails = [...document.querySelectorAll('nav[aria-label="Breadcrumb"]')]
    .filter((el) => el.checkVisibility())
    .map((el) => el.textContent.trim());
  return {header: Math.round(bar.top), over, pinned, trails,
    sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth};
}"""

# What the viewer paints, beside what HQ paints.
_REFERENCE_PAINT = """() => {
  const behind = (el) => {
    for (let up = el; up; up = up.parentElement) {
      const color = getComputedStyle(up).backgroundColor;
      if (!/rgba\\(0, 0, 0, 0\\)|transparent/.test(color)) return color;
    }
    return '';
  };
  const title = document.querySelector('main main h1');
  const page = getComputedStyle(document.body);
  return {
    mode: document.body.className,
    page: page.backgroundColor, viewer: behind(title),
    ink: page.color, title: getComputedStyle(title).color,
    face: page.fontFamily, title_face: getComputedStyle(title).fontFamily,
    header: behind(document.querySelector('header')), sidebar: behind(document.querySelector('aside')),
  };
}"""

_REFERENCE_CHROME = """() => ({
  said: document.querySelector('main').innerText,
  // Every link drawn anywhere on the page, the viewer's sidebar and its foot included.
  leaves: [...document.links]
    .filter((a) => a.origin !== location.origin && a.checkVisibility())
    .map((a) => a.href),
  search: document.querySelector('aside button').innerText,
})"""

# Follows a sidebar entry, as a click, and answers the address it sends the page to.
_REFERENCE_FOLLOW = f"""(path) => {{
  const link = ({_REFERENCE_ENTRY})(path);
  if (!link) return null;
  link.click();
  return link.hash;
}}"""

# Where the page stands on an address: the heading of the section it names, once
# the page is at that address. The viewer names a section by the address under
# its document's slug, so the section is found by the end of its id.
_REFERENCE_HEADING = """(address) => {
  if (location.hash !== address) return null;
  const name = '/' + decodeURIComponent(address.slice(1));
  const section = [...document.querySelectorAll('main main section[id]')].find((s) => s.id.endsWith(name));
  const heading = section && section.querySelector('h2, h3');
  if (!heading || !heading.checkVisibility()) return null;
  return {top: Math.round(heading.getBoundingClientRect().top), title: heading.textContent,
    floor: Math.round(document.querySelector('header').getBoundingClientRect().bottom),
    window: innerHeight};
}"""

# The page is at the address and its heading has stopped moving: the same place
# on three polls running. A busy browser can take longer than that to act on a
# click, and until it does the page is still at the address it had before.
_REFERENCE_AT_REST = f"""(address) => {{
  const found = ({_REFERENCE_HEADING})(address);
  if (!found) return false;
  const last = (window.resting ??= {{}})[address] ?? {{runs: 0}};
  window.resting[address] = {{top: found.top, runs: last.top === found.top ? last.runs + 1 : 0}};
  return window.resting[address].runs >= 3;
}}"""

_REFERENCE_OPEN_MENU = "(root) => document.querySelector(root).querySelector('header button').click()"

_REFERENCE_LISTED = f"(path) => ({_REFERENCE_ENTRY})(path) !== null"


def reference_outline(document):
    """The sidebar HQ's own document declares: groups, and the domains under each."""

    under: dict[str, list[str]] = {}
    for tag in document["tags"]:
        if "parent" in tag:
            under.setdefault(tag["parent"], []).append(tag["summary"])
    return [
        [tag["summary"], under.get(tag["name"], [])]
        for tag in document["tags"]
        if "parent" not in tag and tag.get("kind") != "badge"
    ]


def render_reference():
    """The reference in each theme, the document its viewer reads and the count
    the header asks for, by path."""

    with transaction.atomic():
        client = Client()
        client.force_login(get_user_model().objects.create_user(username="operator"))
        page = reverse("api_reference:reference")
        pages = {
            url.strip("/"): answered(client.get(url))
            for url in (page, reverse("hq_api:openapi"), reverse("action_item_count"))
        }
        client.post(reverse("theme"), {"theme": "dark"})
        pages[REFERENCE_DARK] = answered(client.get(page))
        transaction.set_rollback(True)
    return pages


class ReferenceBrowserTests(BrowserGate):
    """The API reference reads as a page of HQ's, at every width and in both themes."""

    scripted = True

    @classmethod
    def render(cls):
        pages = render_reference()
        cls.document = json.loads(pages[reverse("hq_api:openapi").strip("/")])
        return pages

    def start(self, **options):
        super().start(**options)
        self.requests = []
        self.refused = []
        self.page.on("request", lambda request: self.requests.append(request.url))
        self.page.on(
            "response",
            lambda response: self.refused.append(response.url) if response.status >= 400 else None,
        )

    def visit(self, width, name=REFERENCE, fragment=""):
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.goto(f"{ORIGIN}/{name}/{fragment}", wait_until="load")
        wait_for_reference(self.page)

    def frame(self):
        return self.page.evaluate(_REFERENCE_FRAME, SELECTORS["reference"])

    def assert_framed(self, frame):
        """HQ's header on top, the viewer's pinned parts below it, no trail, no sideways scroll."""

        self.assertEqual(frame["header"], 0)
        self.assertEqual(frame["over"], [])
        self.assertEqual(frame["pinned"], [])
        self.assertEqual(frame["trails"], [])
        self.assertLessEqual(frame["sideways"], 1)

    def test_the_sidebar_is_hqs_navigation(self):
        """The groups and domains the document declares, in its order, and nothing else:
        no section for the version badge, none for the schemas."""

        self.visit(WIDE)
        self.page.evaluate(_REFERENCE_OPEN_GROUPS)
        self.page.wait_for_function(_REFERENCE_GROUPS_OPEN)
        drawn = [
            [title, under]
            for href, title, under in self.page.evaluate(_REFERENCE_OUTLINE)
            if not href.startswith("#description")
        ]
        self.assertEqual(drawn, reference_outline(self.document))
        self.assertTrue(any(under for _, under in drawn))

    def test_the_header_is_never_covered_and_no_trail_is_shown(self):
        for width in REFERENCE_DESKTOP:
            self.visit(width)
            height = self.page.evaluate("() => document.documentElement.scrollHeight")
            for offset in (0, 600, height // 2, height):
                with self.subTest(width=width, offset=offset):
                    self.page.evaluate("(y) => window.scrollTo(0, y)", offset)
                    self.settle()
                    self.assert_framed(self.frame())

    def test_a_phone_reaches_the_sidebar_under_the_header(self):
        self.visit(REFERENCE_PHONE)
        self.assert_framed(self.frame())
        self.page.evaluate(_REFERENCE_OPEN_MENU, SELECTORS["reference"])
        self.settle()
        self.assert_framed(self.frame())
        entries = self.page.evaluate(_REFERENCE_OUTLINE)
        self.assertGreater(len(entries), 1)

    def assert_painted_as_hq(self, paint, mode):
        self.assertIn(mode, paint["mode"].split())
        self.assertEqual(paint["viewer"], paint["page"])
        # The header lets the page show through its surface; the sidebar is that
        # surface, so they match in color whatever the header's opacity.
        self.assertEqual(_opaque(paint["sidebar"]), _opaque(paint["header"]))
        self.assertEqual(paint["title"], paint["ink"])
        self.assertEqual(paint["title_face"], paint["face"])

    def test_both_themes_are_painted_from_hqs_tokens(self):
        """The system's theme when none is chosen, the chosen one when it is."""

        painted = {}
        for scheme in ("light", "dark"):
            with self.subTest(scheme=scheme):
                self.start(color_scheme=scheme)
                self.visit(WIDE)
                painted[scheme] = self.page.evaluate(_REFERENCE_PAINT)
                self.assert_painted_as_hq(painted[scheme], f"{scheme}-mode")
                report = self.page.evaluate(
                    _CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST]
                )
                self.assertGreater(report["sampled"], 0)
                self.assertEqual(report["found"], [])
        self.assertNotEqual(painted["light"]["page"], painted["dark"]["page"])
        with self.subTest(chosen="dark", system="light"):
            self.start(color_scheme="light")
            self.visit(WIDE, REFERENCE_DARK)
            self.assertEqual(self.page.evaluate(_REFERENCE_PAINT), painted["dark"])

    def test_the_viewer_offers_only_what_works_here(self):
        """One search that says it is the reference's, no request sent from the page,
        no token asked for, no way out to the vendor, and nothing fetched from it."""

        self.visit(REFERENCE_PHONE)
        self.page.evaluate(_REFERENCE_OPEN_MENU, SELECTORS["reference"])
        self.settle()
        self.assertEqual(self.page.evaluate(_REFERENCE_CHROME)["leaves"], [])
        self.visit(WIDE)
        chrome = self.page.evaluate(_REFERENCE_CHROME)
        for words in ("Test Request", "Bearer Token", "Open API Client", "Powered by", "Client Libraries"):
            with self.subTest(words=words):
                self.assertNotIn(words, chrome["said"])
        self.assertEqual(chrome["leaves"], [])
        self.assertIn("Search the reference", chrome["search"])
        self.assertFalse(chrome["search"].strip().lower().endswith("k"))
        self.assertEqual([url for url in self.requests if not url.startswith(ORIGIN)], [])
        self.assertEqual(self.refused, [])

    def test_a_link_to_an_operation_survives_a_reload(self):
        tag = next(tag for tag in self.document["tags"] if "parent" in tag)
        title = next(
            operation["summary"]
            for item in self.document["paths"].values()
            for operation in item.values()
            if operation["tags"][0] == tag["name"]
        )

        def landed(address):
            self.page.wait_for_function(_REFERENCE_AT_REST, arg=address, polling=100)
            heading = self.page.evaluate(_REFERENCE_HEADING, address)
            self.assertIn(title, heading["title"])
            self.assertGreaterEqual(heading["top"], heading["floor"])
            self.assertLess(heading["top"], heading["window"] * 0.6)

        group = next(group["summary"] for group in self.document["tags"] if group["name"] == tag["parent"])
        # A reader opens the group, then the domain in it, then the operation.
        self.visit(WIDE)
        self.assertTrue(self.page.evaluate(_REFERENCE_FOLLOW, [group]))
        self.page.wait_for_function(_REFERENCE_LISTED, arg=[group, tag["summary"]])
        domain = self.page.evaluate(_REFERENCE_FOLLOW, [group, tag["summary"]])
        self.assertTrue(domain)
        self.page.wait_for_function(_REFERENCE_LISTED, arg=[group, tag["summary"], title])
        self.page.wait_for_function(_REFERENCE_AT_REST, arg=domain, polling=100)
        fragment = self.page.evaluate(_REFERENCE_FOLLOW, [group, tag["summary"], title])
        self.assertTrue(fragment)
        landed(fragment)
        self.page.reload(wait_until="load")
        wait_for_reference(self.page)
        self.assertEqual(self.page.evaluate("() => location.hash"), fragment)
        landed(fragment)
        self.assertEqual(self.refused, [])
