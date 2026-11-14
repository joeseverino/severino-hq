"""The API reference: a vendored viewer in HQ's frame and palette.

The viewer's own markup is the vendor's (``estate._NOT_HQS_MARKUP``), so the
gate reads only what HQ gives the page: its frame, its loading line, its
palette, its sidebar's outline and what it lets the viewer reach. The walk
helpers here are dev:look's, which opens the reference on request."""

import difflib
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

from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.serving import ORIGIN
from hq.platform.core.tests.browser.widths import WIDE


def _opaque(color: str) -> tuple[int, ...]:
    """A computed color's red, green and blue, without its opacity, as 0-255:
    ``rgb(255, 255, 255)`` and ``color(srgb 1 1 1 / 0.8)`` are one color."""

    numbers = [float(n) for n in re.findall(r"[\d.]+", color.split("/", maxsplit=1)[0])]
    if color.startswith("color("):
        return tuple(round(n * 255) for n in numbers[:3])
    return tuple(round(n) for n in numbers[:3])


# The API reference: a vendored viewer mounted inside HQ's frame. Its markup is
# the vendor's, so what reads it finds it by tag and ARIA, never by class.
REFERENCE = "api/docs"

REFERENCE_DARK = "api/docs/dark"

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

    schema = _resolved(document, part).get("content", {}).get(_JSON, {}).get("schema")
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
    (``/<METHOD><path>.body``, ``/<METHOD><path>.responses.<status>``).
    ``shared`` are the properties inside those parts left closed, since the
    schema each shows is opened where the walk first met it, each as its
    part's top property's id end and the names of the rows below it
    (``/<METHOD><path>.responses.200.data :: items/subject``)."""

    name: str
    label: str
    tags: tuple[str, ...]
    parts: tuple[str, ...]
    shared: tuple[str, ...] = ()


def opened_walk(document):
    """Each tag of ``document`` that holds operations, in the reference's
    order, with every body and response whose schema no earlier one in the
    walk drew: a schema the tags share is opened once, where it is first
    met, and judged there. So is a schema a property refers to, at any
    depth: the viewer draws it again at every use, and the whole reference
    opened at each would be many times the page."""

    operations: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            operations.setdefault(operation["tags"][0], []).append((method.upper(), path, operation))
    seen: set[str] = set()
    walk = []
    for tag in document["tags"]:
        if tag["name"] not in operations:
            continue
        parts: list[str] = []
        shared: list[str] = []
        for method, path, operation in operations[tag["name"]]:
            for where, part in _drawn(operation):
                key = _schema_key(document, part)
                if key and key not in seen:
                    seen.add(key)
                    parts.append(f"/{method}{path}{where}")
                    schema = _resolved(document, part).get("content", {}).get(_JSON, {}).get("schema", {})
                    shared += _shared_inside(document, f"/{method}{path}{where}", schema, seen)
        walk.append(_opened(document, tag["name"], tag["name"], "", parts, shared))
    return walk


def _resolved(document, node):
    """``node``, its reference followed until it is the thing itself."""

    while isinstance(node, dict) and "$ref" in node:
        found = document
        for step in node["$ref"].removeprefix("#/").split("/"):
            found = found[step]
        node = found
    return node


def _rows_of(document, schema):
    """The schema whose properties the viewer draws under ``schema``'s row,
    through a reference, an array's items and a choice's first variant (the
    one it shows), with the last reference it went through, or ``None``."""

    reference = None
    for _depth in range(32):
        if "$ref" in schema:
            reference = schema["$ref"]
            schema = _resolved(document, schema)
        elif "properties" in schema:
            break
        elif isinstance(schema.get("items"), dict):
            schema = schema["items"]
        elif variants := [each for each in schema.get("anyOf", schema.get("oneOf", [])) if each.get("type") != "null"]:
            schema = variants[0]
        else:
            break
    return reference, schema


def _shared_inside(document, part, schema, seen):
    """The rows inside ``part`` (its ``schema``) that show a schema ``seen``
    already holds, each as ``OpenedTag.shared`` writes it; every other
    schema met is added to ``seen`` as it is first met."""

    reference, top = _rows_of(document, schema)
    if reference:
        seen.add(reference)
    shared: list[str] = []

    def visit(row, place, names):
        reference, below = _rows_of(document, row)
        if not below.get("properties"):
            return
        if reference in seen:
            shared.append(f"{place} :: {'/'.join(names)}")
            return
        if reference:
            seen.add(reference)
        for name, child in below["properties"].items():
            visit(child, place, [*names, name])

    for name, row in top.get("properties", {}).items():
        visit(row, f"{part}.{name}", [])
    return shared


def _drawn(operation):
    """The parts of ``operation`` the viewer opens, its body and each
    response, by the end of the id it gives each after the operation's own."""

    drawn = [(".body", operation["requestBody"])] if "requestBody" in operation else []
    return drawn + [(f".responses.{status}", response) for status, response in operation["responses"].items()]


def _opened(document, tag_name, name, label, parts, shared=()):
    """``parts`` of the tag ``tag_name`` opened, but for the rows ``shared``
    names, under ``name``, labeled as the sidebar names the tag (its group
    first), after ``label`` if given."""

    summary = {tag["name"]: tag for tag in document["tags"]}
    tag = summary[tag_name]
    group = summary.get(tag.get("parent", ""))
    sidebar = f"{group['summary']} / {tag['summary']}" if group else tag["summary"]
    return OpenedTag(
        name=name,
        label=f"{label} in {sidebar}" if label else sidebar,
        tags=tuple(scalar_slug(each) for each in ([group["name"]] if group else []) + [tag_name]),
        parts=tuple(parts),
        shared=tuple(shared),
    )


def opened_operation(document, asked):
    """The operation ``asked`` (``GET /api/v2/...``) opened alone: its tag,
    and its body and every response with each property in them, whatever
    another operation draws. One the document does not hold is refused with
    the operations it does nearest the name."""

    method, _, path = asked.strip().partition(" ")
    operation = document["paths"].get(path.strip(), {}).get(method.lower())
    if operation is None:
        held = [f"{verb.upper()} {at}" for at, item in document["paths"].items() for verb in item]
        nearest = difflib.get_close_matches(asked, held, n=8, cutoff=0.5)
        raise LookupError(f"no operation {asked!r} in the API document; it holds " + (", ".join(nearest) or "none"))
    path, method = path.strip(), method.upper()
    parts = [f"/{method}{path}{where}" for where, _ in _drawn(operation)]
    return _opened(document, operation["tags"][0], f"{method} {path}", f"{method} {path}", parts)


# One round of opening a tag: the "Show More" of each of its slugs, then each
# collapsed disclosure inside a part it opens, the part's own button or a
# property's toggle in it, each pressed once. The viewer gives ids only to a
# part's button and its top properties, so an entry's place is the first such
# id met going out from it: its own, else the first inside the nearest box
# around it that holds one. A popup names what it opens (`aria-haspopup`,
# `aria-controls`: an operation's auth badge, a property's default and
# example lists) and closes on a press elsewhere, so it is left closed, and so
# is a row ``shared`` names: its place, then the names of the rows from there
# down to it. What a press opens the viewer draws once its own queue has run,
# so the presses go on level by level, until a level draws nothing more to
# press, before the page is next laid out. How many were pressed is the answer.
_OPEN_ROUND = """async ([root, more, tags, parts, shared]) => {
  const viewer = document.querySelector(root);
  const pressed = (window.lookPressed ??= new WeakSet());
  const within = (id, part) => {
    const at = id.indexOf(part);
    return at >= 0 && (at + part.length === id.length || id[at + part.length] === '.');
  };
  const placeOf = (entry) => {
    for (let box = entry; box && box !== viewer; box = box.parentElement) {
      const id = box.id.includes('/tag/') ? box.id : box.querySelector('[id*="/tag/"]')?.id;
      if (id) return [id, box];
    }
    return ['', null];
  };
  // A row's name is what labels its toggle.
  const named = (row) => {
    const toggle = row.querySelector(':scope > button[aria-labelledby]');
    return toggle ? document.getElementById(toggle.getAttribute('aria-labelledby'))?.textContent.trim() : '';
  };
  const rowPath = (button, place, box) => {
    const names = [];
    for (let row = button.closest('li'); row && row !== box && box.contains(row); row = row.parentElement.closest('li')) {
      names.unshift(named(row));
    }
    return `${place} :: ${names.join('/')}`;
  };
  const wanted = (button) => {
    if (!button.hasAttribute('aria-expanded')) {
      return button.textContent.trim() === more && tags.some((slug) => button.id.endsWith(`/tag/${slug}`));
    }
    const [place, box] = placeOf(button);
    if (button.getAttribute('aria-expanded') !== 'false' || !parts.some((part) => within(place, part))) return false;
    const row = shared.length && button.hasAttribute('aria-labelledby') ? rowPath(button, place, box) : '';
    return !shared.some((closed) => row.endsWith(closed));
  };
  const level = () => {
    const shut = [...viewer.querySelectorAll('button')].filter((button) => !pressed.has(button)
      && !button.matches('[aria-haspopup], [aria-controls]') && button.checkVisibility() && wanted(button));
    shut.forEach((button) => { pressed.add(button); button.click(); });
    return shut.length;
  };
  let count = 0;
  for (let found = level(); found; found = level()) {
    count += found;
    // The viewer's queue runs as microtasks, some queueing more.
    for (let tick = 0; tick < 10; tick += 1) await null;
  }
  return count;
}"""

# Which of a tag's slugs and parts the viewer drew nothing for: none, unless
# its markup has moved from what the look reads.
_UNDRAWN = """([root, tags, parts]) => {
  const ids = [...document.querySelector(root).querySelectorAll('[id*="/tag/"]')].map((el) => el.id);
  return [...tags.filter((slug) => !ids.some((id) => id.endsWith(`/tag/${slug}`))),
    ...parts.filter((part) => !ids.some((id) => id.includes(part)))];
}"""


def open_tag(page, opened):
    """``opened`` opened as a reader opens it, round after round as each
    press draws what it shows, every entry drawn; the slugs and parts the
    viewer drew nothing for, none when the look reached all of it."""

    arg = [SELECTORS["reference"], SHOW_MORE, list(opened.tags), list(opened.parts), list(opened.shared)]
    for _round in range(20):
        draw_whole_reference(page)
        if not page.evaluate(_OPEN_ROUND, arg):
            break
    draw_whole_reference(page)
    return page.evaluate(_UNDRAWN, [SELECTORS["reference"], list(opened.tags), list(opened.parts)])


# Where the page and the viewer's mount stand, and the window's foot.
_LOADING_PLACE = """([root, content]) => {
  const page = document.querySelector(content).getBoundingClientRect();
  return {page: page.top, foot: page.bottom, mount: document.querySelector(root).getBoundingClientRect().top,
          window: innerHeight};
}"""

# Every stylesheet the page links has arrived: a box read before then is drawn
# by no rule of the page's yet.
_STYLED = "() => [...document.querySelectorAll('link[rel=stylesheet]')].every((link) => link.sheet)"

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


def reference_outline(document):
    """The sidebar HQ's own document declares: each group, the summaries of
    its own operations in the document's order, and the domains under it."""

    under: dict[str, list[str]] = {}
    for tag in document["tags"]:
        if "parent" in tag:
            under.setdefault(tag["parent"], []).append(tag["summary"])
    operations: dict[str, list[str]] = {}
    for item in document["paths"].values():
        for operation in item.values():
            operations.setdefault(operation["tags"][0], []).append(operation["summary"])
    return [
        (tag["summary"], operations.get(tag["name"], []), under.get(tag["name"], []))
        for tag in document["tags"]
        if "parent" not in tag
    ]


def _named(role, name):
    """An ARIA snapshot node of ``role`` and exactly ``name``, quoted as YAML."""

    return _quoted(f"{role} {json.dumps(name)}")


def _quoted(node):
    """An ARIA snapshot node, quoted as YAML."""

    return "'" + node.replace("'", "''") + "'"


def sidebar_snapshot(outline):
    """The sidebar ``outline`` declares with every group open, as an ARIA
    snapshot: the viewer's introduction, then each group, and under a group
    its own operations, each a link named for its summary first, then each
    domain that opens in turn, both lists exactly those entries. A domain's
    operations are left to the viewer."""

    lines = ["- complementary:", "  - list:", "    - /children: equal", "    - listitem:", "      - link /.*/:"]
    lines.append("        - /url: /^#description\\//")
    for group, operations, domains in outline:
        lines += ["    - listitem:", f"      - {_named('link', group)}", "      - button [expanded]"]
        if operations or domains:
            lines += ["      - list:", "        - /children: equal"]
            for operation in operations:
                # The pattern is read as a JavaScript one, so only its syntax is escaped.
                pattern = re.sub(r"[.*+?^${}()|[\]\\/]", r"\\\g<0>", operation)
                lines += ["        - listitem:", f"          - {_quoted(f'link /^{pattern} /')}"]
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
    """The API reference is a page of HQ's: in its frame, under its loading
    line, in its palette, with its outline, reaching nothing but HQ. Each
    test opens it once, at the widest width, where every part of the frame
    shows. A refusal by the page's policy fails any of them (``BrowserGate``)."""

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

    def visit(self, name=REFERENCE):
        self.page.set_viewport_size({"width": WIDE, "height": 900})
        self.page.goto(f"{ORIGIN}/{name}/", wait_until="load")
        wait_for_reference(self.page)

    def sidebar(self):
        """The viewer's sidebar, by its role."""

        return self.page.locator(SELECTORS["reference"]).get_by_role("complementary")

    def test_the_sidebar_is_hqs_navigation(self):
        """The groups the document declares, each with its own operations, then
        its domains, in its order, and nothing else: none for the schemas."""

        outline = reference_outline(self.document)
        self.assertTrue(any(under for _, _, under in outline))
        self.assertTrue(any(own and under for _, own, under in outline))
        self.visit()
        sidebar = self.sidebar()
        # Each group's control, opened as a reader opens it. A group's control
        # comes before the domains it shows, so the first of its name is its own.
        for group, _, _ in outline:
            control = sidebar.get_by_role("button", name=re.compile(rf" - {re.escape(group)}$")).first
            if control.get_attribute("aria-expanded") == "false":
                control.click()
            expect(control).to_have_attribute("aria-expanded", "true")
        expect(sidebar).to_match_aria_snapshot(sidebar_snapshot(outline))

    def test_the_header_is_never_covered_and_no_trail_is_shown(self):
        """HQ's header on top at the page's head and halfway down, the viewer's
        pinned parts just below it, no trail, no sideways scroll."""

        self.visit()
        height = self.page.evaluate("() => document.documentElement.scrollHeight")
        for offset in (0, height // 2):
            with self.subTest(offset=offset):
                self.page.evaluate("(y) => window.scrollTo(0, y)", offset)
                self.settle()
                frame = self.page.evaluate(_REFERENCE_FRAME, SELECTORS["reference"])
                self.assertEqual(frame["header"], 0)
                self.assertEqual(frame["over"], [])
                self.assertEqual(frame["pinned"], [])
                # Pinned at the header's foot: no higher, where the header would
                # hide it, and no lower, where the page would show through.
                self.assertTrue(frame["lines"])
                for line in frame["lines"]:
                    self.assertAlmostEqual(line, frame["floor"], delta=1)
                self.assertEqual(frame["trails"], [])
                self.assertLessEqual(frame["sideways"], 1)

    def test_the_loading_line_holds_the_references_place(self):
        """While the viewer's bundle is on its way, the server's HTML already
        holds the place the reference takes: the page reaches the window's
        foot, and the viewer draws where the page and its mount already stand,
        so nothing of HQ's moves as it does."""

        bundle = f"**{static('vendor/scalar/standalone.js')}"
        held = []

        # A function of its own: Playwright marks the handler it is given,
        # which a list's bound append cannot carry.
        def hold(route):
            held.append(route)

        self.page.route(bundle, hold)
        place = [SELECTORS["reference"], SELECTORS["content"]]
        self.page.set_viewport_size({"width": WIDE, "height": 900})
        with self.page.expect_request(bundle):
            self.page.goto(f"{ORIGIN}/{REFERENCE}/", wait_until="commit")
        expect(self.page.locator(SELECTORS["content"]).get_by_role("status")).to_be_visible()
        self.page.wait_for_function(_STYLED)
        loading = self.page.evaluate(_LOADING_PLACE, place)
        while held:
            held.pop().fallback()
        wait_for_reference(self.page)
        drawn = self.page.evaluate(_LOADING_PLACE, place)
        self.assertGreaterEqual(loading["foot"], loading["window"] - 1)
        self.assertEqual((drawn["page"], drawn["mount"]), (loading["page"], loading["mount"]))
        self.page.unroute(bundle)

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
                self.visit()
                painted[scheme] = self.page.evaluate(_REFERENCE_PAINT)
                self.assert_painted_as_hq(painted[scheme], f"{scheme}-mode")
        self.assertNotEqual(painted["light"]["page"], painted["dark"]["page"])
        with self.subTest(chosen="dark", system="light"):
            self.start(color_scheme="light")
            self.visit(REFERENCE_DARK)
            self.assertEqual(self.page.evaluate(_REFERENCE_PAINT), painted["dark"])

    def test_the_viewer_offers_only_what_works_here(self):
        """One search that says it is the reference's, no request sent from the page,
        no token asked for, no way out to the vendor, and every address it asks
        for answered."""

        self.visit()
        chrome = self.page.evaluate(_REFERENCE_CHROME)
        for words in ("Test Request", "Bearer Token", "Open API Client", "Powered by", "Client Libraries"):
            with self.subTest(words=words):
                self.assertNotIn(words, chrome["said"])
        self.assertEqual(chrome["leaves"], [])
        self.assertIn("Search the reference", chrome["search"])
        self.assertFalse(chrome["search"].strip().lower().endswith("k"))
        self.assertEqual([url for url in self.requests if not url.startswith(ORIGIN)], [])
        self.assertEqual(self.refused, [])
