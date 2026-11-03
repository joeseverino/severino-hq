"""The API reference: a vendored viewer, held to the rules its frame and palette must keep."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, override

from django.contrib.auth import get_user_model
from django.db import transaction
from django.templatetags.static import static
from django.test import Client
from django.urls import reverse
lazy from playwright.sync_api import expect

from hq.platform.application.tests.writers import form_keyed
from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.probes.axe import contrast_read
from hq.platform.core.tests.browser.probes.describe import CUTS, DESCRIBE, TEXT_SHOWN
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE, WIDTHS


def _opaque(color: str) -> tuple[int, ...]:
    """A computed color's red, green and blue, without its opacity, as 0-255:
    ``rgb(255, 255, 255)`` and ``color(srgb 1 1 1 / 0.8)`` are one color."""

    numbers = [float(n) for n in re.findall(r"[\d.]+", color.split("/", maxsplit=1)[0])]
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


# The viewer draws an entry once it nears the view and holds the place of one
# not drawn yet with a blank box (`data-placeholder` on its Lazy wrapper), so a
# shot of the whole page shows blank bands where no reader ever sees one. Each
# is brought into view until none is left, then the page goes back to its top;
# how many were left is the answer, 0 once the whole reference is drawn.
_DRAW_WHOLE = """async (root) => {
  const viewer = document.querySelector(root);
  const frame = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  for (let round = 0; round < 1000; round += 1) {
    const blank = viewer.querySelector('[data-placeholder="true"]');
    if (!blank) break;
    blank.scrollIntoView({block: 'center'});
    await frame();
  }
  window.scrollTo(0, 0);
  await frame();
  return viewer.querySelectorAll('[data-placeholder="true"]').length;
}"""


def draw_whole_reference(page):
    """Every entry of the reference drawn, as a reader scrolling through it sees each."""

    return page.evaluate(_DRAW_WHOLE, SELECTORS["reference"])


# The label of the viewer's button that shows the rest of a group or a tag
# (`actions.showMore`, its ShowMoreButton), which says nothing in ARIA.
SHOW_MORE = "Show More"

# The media type whose schema the viewer draws for a body or a response.
_JSON = "application/json"


def scalar_slug(name):
    """A tag's name as the viewer writes it in the ids it gives the tag's
    entries (`api-1/tag/<slug>/...`): lowercased, every character but a
    letter, number, space, `_` or `-` dropped, each run of those three one
    `-`, none at either end (the slugger of Scalar's standalone bundle)."""

    slug = unicodedata.normalize("NFC", name[:255].strip()).lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"[\s_-]+", "-", slug).strip("-")


def served_document(page):
    """The document the viewer on ``page`` was given, fetched from the address it read it from."""

    return page.evaluate(_SERVED, SELECTORS["reference"])


_SERVED = "async (root) => (await fetch(document.querySelector(root).dataset.url)).json()"


def _schema_key(document, part):
    """What a body or a response draws, as one key: the schema's reference,
    or a digest of the schema written in place; a response named by
    reference is first resolved."""

    while "$ref" in part:
        node = document
        for step in part["$ref"].removeprefix("#/").split("/"):
            node = node[step]
        part = node
    schema = part.get("content", {}).get(_JSON, {}).get("schema")
    if schema is None:
        return ""
    if "$ref" in schema:
        return schema["$ref"]
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class OpenedTag:
    """One tag of the reference opened as the look opens it.

    ``label`` names it as the sidebar does (its group first), ``tags`` are
    the slugs whose "Show More" a reader presses to reach its operations
    (its group's, then its own), and ``parts`` the bodies and responses
    opened, each as the end of the id the viewer gives it
    (``/<METHOD><path>.body``, ``/<METHOD><path>.responses.<status>``)."""

    name: str
    label: str
    tags: tuple[str, ...]
    parts: tuple[str, ...]


def opened_walk(document):
    """Each tag of ``document`` that holds operations, in the reference's
    order, with every body and response whose schema no earlier one in the
    walk drew: a schema the tags share is opened once, where it is first
    met, and judged there."""

    summary = {tag["name"]: tag for tag in document["tags"]}
    operations: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            operations.setdefault(operation["tags"][0], []).append((method.upper(), path, operation))
    seen: set[str] = set()
    walk = []
    for tag in document["tags"]:
        if tag["name"] not in operations:
            continue
        parts = []
        for method, path, operation in operations[tag["name"]]:
            drawn = [(".body", operation["requestBody"])] if "requestBody" in operation else []
            drawn += [(f".responses.{status}", response) for status, response in operation["responses"].items()]
            for where, part in drawn:
                key = _schema_key(document, part)
                if key and key not in seen:
                    seen.add(key)
                    parts.append(f"/{method}{path}{where}")
        group = summary.get(tag.get("parent", ""))
        walk.append(
            OpenedTag(
                name=tag["name"],
                label=f"{group['summary']} / {tag['summary']}" if group else tag["summary"],
                tags=tuple(scalar_slug(name) for name in ([group["name"]] if group else []) + [tag["name"]]),
                parts=tuple(parts),
            )
        )
    return walk


# One round of opening a tag: the "Show More" of each of its slugs, then each
# collapsed disclosure inside a part it opens, the part's own button or a
# property's toggle in it, each pressed once. The viewer gives ids only to a
# part's button and its top properties, so an entry's place is the first such
# id met going out from it: its own, else the first inside the nearest box
# around it that holds one. A popup names what it opens (`aria-haspopup`,
# `aria-controls`: an operation's auth badge, a property's default and
# example lists) and closes on a press elsewhere, so it is left closed. How
# many were pressed is the answer.
_OPEN_ROUND = """([root, more, tags, parts]) => {
  const viewer = document.querySelector(root);
  const pressed = (window.lookPressed ??= new WeakSet());
  const within = (id, part) => {
    const at = id.indexOf(part);
    return at >= 0 && (at + part.length === id.length || id[at + part.length] === '.');
  };
  const placeOf = (entry) => {
    for (let box = entry; box && box !== viewer; box = box.parentElement) {
      const id = box.id.includes('/tag/') ? box.id : box.querySelector('[id*="/tag/"]')?.id;
      if (id) return id;
    }
    return '';
  };
  const wanted = (button) => {
    if (!button.hasAttribute('aria-expanded')) {
      return button.textContent.trim() === more && tags.some((slug) => button.id.endsWith(`/tag/${slug}`));
    }
    const place = placeOf(button);
    return button.getAttribute('aria-expanded') === 'false' && parts.some((part) => within(place, part));
  };
  const shut = [...viewer.querySelectorAll('button')].filter((button) => !pressed.has(button)
    && !button.matches('[aria-haspopup], [aria-controls]') && button.checkVisibility() && wanted(button));
  shut.forEach((button) => { pressed.add(button); button.click(); });
  return shut.length;
}"""

# Which of a tag's slugs and parts the viewer drew nothing for: none, unless
# its markup has moved from what the look reads.
_UNDRAWN = """([root, tags, parts]) => {
  const ids = [...document.querySelector(root).querySelectorAll('[id*="/tag/"]')].map((el) => el.id);
  return [...tags.filter((slug) => !ids.some((id) => id.endsWith(`/tag/${slug}`))),
    ...parts.filter((part) => !ids.some((id) => id.includes(part)))];
}"""


def every_tag(document):
    """Every tag of ``document`` opened at once, each schema with the tag
    that first draws it: the whole reference as a reader opening it all
    reads it."""

    walk = opened_walk(document)
    return OpenedTag(
        name="every-tag",
        label="every tag",
        tags=tuple(dict.fromkeys(slug for opened in walk for slug in opened.tags)),
        parts=tuple(part for opened in walk for part in opened.parts),
    )


def open_tag(page, opened):
    """``opened`` opened as a reader opens it, round after round as each
    press draws what it shows, every entry drawn; the slugs and parts the
    viewer drew nothing for, none when the look reached all of it."""

    arg = [SELECTORS["reference"], SHOW_MORE, list(opened.tags), list(opened.parts)]
    for _round in range(20):
        draw_whole_reference(page)
        if not page.evaluate(_OPEN_ROUND, arg):
            break
    draw_whole_reference(page)
    return page.evaluate(_UNDRAWN, [SELECTORS["reference"], list(opened.tags), list(opened.parts)])


# What stands over HQ's header, what of the viewer's is pinned under it, and
# any trail the viewer is showing.
_REFERENCE_FRAME = """(root) => {
  const header = document.querySelector('header');
  const bar = header.getBoundingClientRect();
  const over = [0.05, 0.25, 0.5, 0.75, 0.95]
    .map((share) => document.elementFromPoint(bar.left + bar.width * share, bar.top + bar.height / 2))
    .filter((el) => !header.contains(el))
    .map((el) => el.tagName.toLowerCase());
  const viewer = document.querySelector(root);
  const stuck = (el) => el.checkVisibility() && ['sticky', 'fixed'].includes(getComputedStyle(el).position);
  const pinned = [...viewer.querySelectorAll('aside, nav, header, [data-scalar-scroll-header]')]
    .filter(stuck)
    .map((el) => [el.tagName.toLowerCase(), Math.round(el.getBoundingClientRect().top)])
    .filter(([, top]) => top < Math.round(bar.bottom) - 1);
  // The line the viewer's navigation pins at: the box that holds its sidebar
  // or its bar in place, the box itself or the nearest one around it.
  const holder = (el) => {
    for (let up = el; up && up !== viewer; up = up.parentElement) if (stuck(up)) return up;
    return null;
  };
  const lines = [...viewer.querySelectorAll('aside, header')]
    .filter((el) => el.checkVisibility())
    .map(holder)
    .filter(Boolean)
    .map((el) => Math.round(parseFloat(getComputedStyle(el).top)));
  const trails = [...document.querySelectorAll('nav[aria-label="Breadcrumb"]')]
    .filter((el) => el.checkVisibility())
    .map((el) => el.textContent.trim());
  return {header: Math.round(bar.top), over, pinned, trails, lines, floor: Math.round(bar.bottom),
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
    window: innerHeight, focused: document.activeElement === section};
}"""

# The viewer has scrolled to the section the address names, which it says by
# focusing that section, and the heading has stopped moving: the same place on
# three polls running. A hold on an entry followed earlier pulls the page away
# within a frame of that scroll, so a page that holds still here stays there.
_REFERENCE_AT_REST = f"""(address) => {{
  const found = ({_REFERENCE_HEADING})(address);
  if (!found || !found.focused) return false;
  const last = (window.resting ??= {{}})[address] ?? {{runs: 0}};
  window.resting[address] = {{top: found.top, runs: last.top === found.top ? last.runs + 1 : 0}};
  return window.resting[address].runs >= 3;
}}"""

# Each run of text in the viewer drawn past a box that clips it, as the
# survey and tables.js read a cut (`CUTS`): the box that cuts it and the text.
# The viewer keeps words for assistive technology alone in a box of one
# pixel that clips them (the visually hidden pattern), so a run cut by such a
# box is not drawn at all, rather than drawn short.
_REFERENCE_CUTS = (
    "([root, unseen]) => {"
    + DESCRIBE
    + CUTS
    + r"""
  const hidden = (box) => box.clientWidth <= 1 && box.clientHeight <= 1;
  return [...new Set(cutsIn(document.querySelector(root), unseen)
    .filter(({boxes}) => !boxes.some(hidden))
    .map(({node, boxes}) => `${describe(boxes[0])} cuts "${node.data.trim().slice(0, 60)}"`))];
}"""
)

# Each box in the viewer a reader scrolls sideways to read what it holds: a
# code sample or a strip of tabs wider than the column.
_REFERENCE_SIDEWAYS = (
    "(root) => {"
    + DESCRIBE
    + r"""
  return [...document.querySelector(root).querySelectorAll('*')]
    .filter((el) => scrolls(el) && el.scrollWidth > el.clientWidth + 1 && el.checkVisibility())
    .map((el) => `${describe(el)} scrolls sideways to show "${el.textContent.trim().slice(0, 60)}"`);
}"""
)

# Each run of text the viewer draws, whole and trimmed.
_REFERENCE_WORDS = (
    "(root) => {"
    + TEXT_SHOWN
    + r"""
  const found = [];
  const walker = document.createTreeWalker(document.querySelector(root), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.data.trim() && textShown(node)) found.push(node.data.trim());
  }
  return found;
}"""
)

# The elements HQ's element rules style as controls.
_CONTROLS = ("button", "input", "select", "textarea", "a")

# Every control in the viewer, drawn as HQ's sheets have it, then again with
# every rule of HQ's that names one of those elements taken out: the sheets the
# page links, the viewer's own configuration (`own`) apart. What changed is a
# property an HQ rule gave a control of the viewer's.
_REFERENCE_CONTROLS = (
    "([root, tags, own]) => {"
    + DESCRIBE
    + r"""
  const controls = [...document.querySelector(root).querySelectorAll(tags.join(', '))];
  const read = () => controls.map((el) => {
    const style = getComputedStyle(el);
    return Object.fromEntries([...style].filter((name) => !name.startsWith('--')).map((name) => [name, style.getPropertyValue(name)]));
  });
  const before = read();
  const names = new RegExp(`(^|[^\\w-])(${tags.join('|')})(?![\\w-])`);
  const strip = (list) => {
    for (let i = list.cssRules.length - 1; i >= 0; i -= 1) {
      const rule = list.cssRules[i];
      if (rule instanceof CSSStyleRule && names.test(rule.selectorText)) list.deleteRule(i);
      else if (rule.cssRules) strip(rule);
    }
  };
  [...document.styleSheets].filter((sheet) => sheet.href && new URL(sheet.href).pathname !== own).forEach(strip);
  const after = read();
  const changed = [];
  controls.forEach((el, i) => {
    for (const [name, value] of Object.entries(before[i])) {
      if (after[i][name] !== value) changed.push(`${describe(el)} ${name}: ${value} (HQ's), ${after[i][name]} (the viewer's)`);
    }
  });
  return {controls: controls.length, changed: [...new Set(changed)].slice(0, 40)};
}"""
)

_REFERENCE_OPEN_MENU = "(root) => document.querySelector(root).querySelector('header button').click()"


def reference_outline(document):
    """The sidebar HQ's own document declares: groups, and the domains under each."""

    under: dict[str, list[str]] = {}
    for tag in document["tags"]:
        if "parent" in tag:
            under.setdefault(tag["parent"], []).append(tag["summary"])
    return [[tag["summary"], under.get(tag["name"], [])] for tag in document["tags"] if "parent" not in tag]


def _named(role, name):
    """An ARIA snapshot node of ``role`` and exactly ``name``, quoted as YAML."""

    return "'" + f"{role} {json.dumps(name)}".replace("'", "''") + "'"


def sidebar_snapshot(outline):
    """The sidebar ``outline`` declares with every group open, as an ARIA
    snapshot: the viewer's introduction, then each group, and under a group
    each domain that opens in turn, both lists exactly those entries. A
    group's operations are left to the viewer."""

    lines = ["- complementary:", "  - list:", "    - /children: equal", "    - listitem:", "      - link /.*/:"]
    lines.append("        - /url: /^#description\\//")
    for group, domains in outline:
        lines += ["    - listitem:", f"      - {_named('link', group)}", "      - button [expanded]"]
        if domains:
            lines += ["      - list:", "        - /children: equal"]
            for domain in domains:
                lines += ["        - listitem:", f"          - {_named('link', domain)}", "          - button"]
    return "\n".join(lines)


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
        client.post(reverse("theme"), form_keyed({"theme": "dark"}))
        pages[REFERENCE_DARK] = answered(client.get(page))
        transaction.set_rollback(True)
    return pages


class ReferenceBrowserTests(BrowserGate):
    """The API reference reads as a page of HQ's, at every width and in both themes."""

    scripted = True
    document: dict[str, Any]

    @classmethod
    @override
    def render(cls):
        pages = render_reference()
        cls.document = json.loads(pages[reverse("hq_api:openapi").strip("/")])
        return pages

    @override
    def start(self, **options):
        super().start(**options)
        self.requests: list[str] = []
        self.refused: list[str] = []
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

    def sidebar(self):
        """The viewer's sidebar, by its role."""

        return self.page.locator(SELECTORS["reference"]).get_by_role("complementary")

    def assert_framed(self, frame):
        """HQ's header on top, the viewer's pinned parts just below it, no trail, no sideways scroll."""

        self.assertEqual(frame["header"], 0)
        self.assertEqual(frame["over"], [])
        self.assertEqual(frame["pinned"], [])
        # Pinned at the header's foot: no higher, where the header would hide it,
        # and no lower, where the page would show through between them.
        self.assertTrue(frame["lines"])
        for line in frame["lines"]:
            self.assertAlmostEqual(line, frame["floor"], delta=1)
        self.assertEqual(frame["trails"], [])
        self.assertLessEqual(frame["sideways"], 1)

    def test_the_sidebar_is_hqs_navigation(self):
        """The groups and domains the document declares, in its order, and nothing else:
        none for the schemas."""

        outline = reference_outline(self.document)
        self.assertTrue(any(under for _, under in outline))
        self.visit(WIDE)
        sidebar = self.sidebar()
        # Each group's control, opened as a reader opens it. A group's control
        # comes before the domains it shows, so the first of its name is its own.
        for group, _ in outline:
            control = sidebar.get_by_role("button", name=re.compile(rf" - {re.escape(group)}$")).first
            if control.get_attribute("aria-expanded") == "false":
                control.click()
            expect(control).to_have_attribute("aria-expanded", "true")
        expect(sidebar).to_match_aria_snapshot(sidebar_snapshot(outline))

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
        self.assertGreater(self.sidebar().get_by_role("link").count(), 1)

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
                self.assertEqual(self.read([contrast_read()])[0], [])
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

    def test_every_path_sample_and_value_reads_whole(self):
        """With every tag opened, no text the viewer draws is cut short, marked
        with an ellipsis or not, at any width: a path, a sample's line and a
        property's type, default or example each read in full."""

        for width in WIDTHS:
            with self.subTest(width=width):
                self.visit(width)
                self.assertEqual(open_tag(self.page, every_tag(self.document)), [])
                self.settle()
                cut = self.page.evaluate(_REFERENCE_CUTS, [SELECTORS["reference"], SELECTORS["unseen"]])
                self.assertEqual(cut, [])
                self.assertEqual(self.page.evaluate(_REFERENCE_SIDEWAYS, SELECTORS["reference"]), [])

    def test_a_field_reads_by_its_type(self):
        """A field, a body and an answer say their type ("string", "object"),
        never the name of the schema they refer to: the schemas are not drawn
        on their own, so a name would point at nothing a reader can open."""

        named = set(self.document["components"]["schemas"])
        first = opened_walk(self.document)[0]
        self.assertTrue(first.parts)
        self.visit(WIDE)
        self.assertEqual(open_tag(self.page, first), [])
        drawn = set(self.page.evaluate(_REFERENCE_WORDS, SELECTORS["reference"]))
        self.assertTrue(drawn)
        self.assertEqual(sorted(named & drawn), [])

    def test_the_viewer_draws_its_own_controls(self):
        """No element rule of HQ's (a button's raised box, a field's height, a
        link's color) reaches a button, field or link the viewer draws: with
        every one of those rules taken out of HQ's sheets, every control in
        the opened reference is drawn as it was."""

        self.visit(REFERENCE_PHONE)
        self.assertEqual(open_tag(self.page, every_tag(self.document)), [])
        self.settle()
        own = static("css/api-reference.css")
        reached = self.page.evaluate(_REFERENCE_CONTROLS, [SELECTORS["reference"], list(_CONTROLS), own])
        self.assertGreater(reached["controls"], 100)
        self.assertEqual(reached["changed"], [])

    def test_a_link_to_an_operation_survives_a_reload(self):
        """The page lands on the entry followed last, however quickly a reader
        follows one after another, and lands there again on a reload."""

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
        # A reader opens the group, then the domain in it, then the operation,
        # each as soon as the sidebar lists it: while the viewer is still
        # drawing what the earlier ones lead to.
        self.visit(WIDE)
        within = self.sidebar()
        for entry in (group, tag["summary"], title):
            # An entry's link is named for it, an operation's with its method
            # after; a click waits until the sidebar lists it.
            named = re.compile(rf"^{re.escape(entry)}( |$)")
            link = within.get_by_role("link", name=named).first
            link.click()
            # The entries under it are in its own item's list: the first item
            # holding its link, since an item comes before the items it holds.
            item = within.get_by_role("listitem").filter(has=self.page.get_by_role("link", name=named)).first
            within = item.get_by_role("list").first
        fragment = link.evaluate("(a) => a.hash")
        self.assertTrue(fragment)
        landed(fragment)
        self.page.reload(wait_until="load")
        wait_for_reference(self.page)
        self.assertEqual(self.page.evaluate("() => location.hash"), fragment)
        landed(fragment)
        self.assertEqual(self.refused, [])
