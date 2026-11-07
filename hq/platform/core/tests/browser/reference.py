"""The API reference: a vendored viewer, held to the rules its frame and palette must keep."""

import contextlib
import difflib
import hashlib
import itertools
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
from hq.platform.core.stylesheets import SHEETS
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

# A laptop's window too narrow for the viewer's sidebar, yet past the width at
# which the viewer stacks a tag's description over its list of operations.
_LAPTOP_WINDOW = 924

# The engines the browser legs draw in (mise-tasks/browser.toml): WebKit, the
# gates' own, and Chromium, which CI's layout legs add. No leg runs Firefox.
GATE_ENGINES = ("webkit", "chromium")

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
        shared=tuple(row for opened in walk for row in opened.shared),
    )


def open_tag(page, opened, step=None):
    """``opened`` opened as a reader opens it, round after round as each
    press draws what it shows, every entry drawn; the slugs and parts the
    viewer drew nothing for, none when the look reached all of it. Each
    round runs inside ``step(round)``, a context a test makes a subtest of,
    so the stall watch sees each one end."""

    arg = [SELECTORS["reference"], SHOW_MORE, list(opened.tags), list(opened.parts), list(opened.shared)]
    for each in range(20):
        # A subtest records a round that fails and goes on: then it has ended.
        pressed = 0
        with step(each) if step else contextlib.nullcontext():
            draw_whole_reference(page)
            pressed = page.evaluate(_OPEN_ROUND, arg)
        if not pressed:
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

# Where the reader stands: the line under the bars pinned over the page (HQ's
# header and any bar of the viewer's held under it), the section drawn just
# below that line and its top, and the entry the sidebar marks as current.
_REFERENCE_PLACE = """(root) => {
  const viewer = document.querySelector(root);
  const pinned = [document.querySelector('header'), ...viewer.querySelectorAll('[data-scalar-scroll-header]')]
    .filter((bar) => bar.checkVisibility() && ['sticky', 'fixed'].includes(getComputedStyle(bar).position));
  const line = Math.max(...pinned.map((bar) => bar.getBoundingClientRect().bottom));
  const content = viewer.querySelector('main').getBoundingClientRect();
  const section = document.elementFromPoint(content.left + content.width / 2, line + 2)?.closest('section[id]');
  return {line: Math.round(line), under: section?.id ?? '',
    top: section ? Math.round(section.getBoundingClientRect().top) : null,
    current: viewer.querySelector('aside [aria-current="page"]')?.getAttribute('href') ?? ''};
}"""

# The links a tag's own section draws, by their address: the section whose id
# ends with the tag's.
_SECTION_LINKS = """([root, tag]) => {
  const section = [...document.querySelector(root).querySelectorAll('main section[id]')].find((s) => s.id.endsWith(tag));
  return section ? [...section.querySelectorAll('a[href^="#"]')].map((a) => a.getAttribute('href')) : null;
}"""

# The left and right edge of each tag's list of operations drawn, to the pixel.
_LIST_EDGES = """(root) => [...document.querySelector(root).querySelectorAll('main [role="group"]:has(ul a)')]
  .filter((card) => card.checkVisibility())
  .map((card) => { const box = card.getBoundingClientRect(); return `${Math.round(box.left)}-${Math.round(box.right)}`; })"""

# Each path in a tag's list of operations drawn over more than one line while
# something else shares the list's row, by its text. The list is the group
# that names it; its row is the box two levels out, which holds the tag's
# description beside it or above it.
_PATHS_SHARING_A_ROW = """(root) => {
  const found = [];
  for (const list of document.querySelector(root).querySelectorAll('main [role="group"] ul')) {
    const card = list.closest('[role="group"]');
    const row = card.parentElement.parentElement.getBoundingClientRect();
    if (!card.checkVisibility() || card.getBoundingClientRect().width >= row.width - 1) continue;
    for (const link of list.querySelectorAll('a')) {
      const path = link.lastElementChild;
      const range = document.createRange();
      range.selectNodeContents(path);
      const lines = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top))).size;
      if (lines > 1) found.push(path.textContent.trim());
    }
  }
  return found;
}"""

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

# Each word with a "/" in it (a path, a URL) that the viewer draws over more
# than one line and starts a line other than at a "/", ".", "-", "?" or "&",
# where Scalar breaks the path in an operation's heading and api-reference.js
# breaks one everywhere else, or just after a query's "?", where both engines
# break one themselves; each line of it shown. A segment between two marks
# wider than its line is cut where it meets the edge (`overflow-wrap`), which
# nothing can avoid, so a line may also start inside a segment that began the
# line before it and still did not fit there. A word is the characters
# between two spaces in one box's lines: a box of its own (a flex item, a
# block) starts a new one. Where a line ends is where the next character
# drawn starts below the last.
_REFERENCE_PATH_BREAKS = (
    "([root, unseen]) => {"
    + TEXT_SHOWN
    + r"""
  const viewer = document.querySelector(root);
  const boxes = new Map();
  const boxOf = (el) => {
    if (!boxes.has(el)) {
      const display = getComputedStyle(el).display;
      boxes.set(el, ['inline', 'contents'].includes(display) && el.parentElement ? boxOf(el.parentElement) : el);
    }
    return boxes.get(el);
  };
  const range = document.createRange();
  const drawnAt = ([node, at]) => {
    range.setStart(node, at);
    range.setEnd(node, at + 1);
    const drawn = range.getBoundingClientRect();
    return drawn.width || drawn.height ? drawn : null;
  };
  const below = (drawn, above) => drawn.top >= above.bottom - 1;
  // Whether a line may end inside `line` after its first character.
  const markIn = (line) => [...line].some((ch, j) =>
    j > 0 && (line[j - 1] === '?' || (/[/.?&-]/.test(ch) && !/[\s/.?&:-]/.test(line[j - 1]))));
  const found = [];
  let word = [];
  let before = null;
  const close = () => {
    if (word.some(([node, at]) => node.data[at] === '/')) {
      const lines = [''];
      let last = null;
      let began = true;
      for (const place of word) {
        const drawn = drawnAt(place);
        if (!drawn) continue;
        if (!last) began = !before || below(drawn, before);
        if (last && below(drawn, last)) lines.push('');
        lines[lines.length - 1] += place[0].data[place[1]];
        last = drawn;
      }
      // Each line, whether it starts where a segment between two marks
      // starts a line of its own: the word's first line when it began a
      // line, a line that starts at a mark, and one inside a segment that
      // began the line before it and still did not fit there.
      const starts = [began];
      for (let i = 1; i < lines.length; i += 1) {
        const atMark = /^[/.?&-]/.test(lines[i]) || lines[i - 1].endsWith('?');
        starts.push(atMark || (starts[i - 1] && !markIn(lines[i - 1])));
      }
      if (starts.slice(1).some((start) => !start)) found.push(lines.join(' | '));
    }
    const end = word.length ? drawnAt(word[word.length - 1]) : null;
    if (end) before = end;
    word = [];
  };
  let box = null;
  const walker = document.createTreeWalker(viewer, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!textShown(node) || node.parentElement.closest(unseen)) continue;
    const holder = boxOf(node.parentElement);
    if (holder !== box) { close(); box = holder; before = null; }
    for (let at = 0; at < node.data.length; at += 1) {
      if (/\s/.test(node.data[at])) close();
      else word.push([node, at]);
    }
  }
  close();
  return [...new Set(found)];
}"""
)

# A path of many hyphenated parts, and one whose last part, a digest, is wider
# than any line of a sample.
_HYPHENATED_PATH = (
    "/example/alpha-bravo-charlie-delta-echo-foxtrot-golf-hotel-india-juliet-kilo-lima-mike-november-oscar-papa/"
)
_DIGEST_PATH = "registry.example.net/example/image@sha512:" + "0123456789abcdef" * 8

# Each path written into a code sample on a line of its own, as the viewer
# writes one; then the text of each as the page holds it, and how many lines
# it is drawn over.
_PLANT_PATHS = """([sample, paths]) => {
  const planted = paths.map((path) => {
    const line = document.createElement('span');
    line.textContent = path;
    document.querySelector(sample).append('\\n', line);
    return line;
  });
  return new Promise((done) => requestAnimationFrame(() => done(planted.map((line) => ({
    text: line.textContent,
    lines: new Set([...line.getClientRects()].map((rect) => Math.round(rect.top))).size,
  })))));
}"""

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

# Every element the viewer draws, and the boxes it draws before and after
# one, read as HQ's sheets have them (`sheets`, the stylesheet's links), then
# again with every rule of those sheets held to the elements outside the
# viewer: each selector, nested ones included, is told `:not(<root> *)` and
# put back after. Nothing outside the viewer changes, so what changed inside
# is a property an HQ rule gave the viewer's markup, named with the element and
# each HQ rule that matches it and says that property (or every one that
# matches, where the property is set by a shorthand with a var() in it).
# Transitions are stopped while it reads, so a change is read at once.
_REFERENCE_REACHED = (
    "([root, sheets]) => {"
    + DESCRIBE
    + r"""
  const split = (list) => {
    const parts = [];
    let depth = 0;
    let start = 0;
    for (let i = 0; i < list.length; i += 1) {
      if ('(['.includes(list[i])) depth += 1;
      else if (')]'.includes(list[i])) depth -= 1;
      else if (list[i] === ',' && !depth) { parts.push(list.slice(start, i).trim()); start = i + 1; }
    }
    return [...parts, list.slice(start).trim()];
  };
  const PSEUDO = /(?:::[\w-]+(?:\([^)]*\))?|:(?:before|after|first-line|first-letter))+$/;
  const subject = (part) => { const m = PSEUDO.exec(part); return m ? [part.slice(0, m.index), m[0]] : [part, '']; };
  const rules = [];
  const collect = (list) => {
    for (const rule of list.cssRules) {
      if (rule instanceof CSSStyleRule) rules.push(rule);
      if (rule.cssRules) collect(rule);
    }
  };
  const app = [...document.styleSheets].filter((sheet) => sheet.href && sheets.includes(new URL(sheet.href).pathname));
  app.forEach(collect);
  const full = (rule) => {
    const own = split(rule.selectorText);
    if (!(rule.parentRule instanceof CSSStyleRule)) return own;
    const parent = full(rule.parentRule).join(', ');
    return own.map((part) => (part.includes('&') ? part.replaceAll('&', `:is(${parent})`) : `:is(${parent}) ${part}`));
  };
  const matches = (rule, el, pseudo) => {
    try {
      return full(rule).some((part) => {
        const [base, own] = subject(part);
        return (own && !own.startsWith('::') ? `:${own}` : own) === pseudo && el.matches(base || '*');
      });
    } catch { return false; }
  };
  const viewer = document.querySelector(root);
  const els = [...viewer.querySelectorAll('*')];
  // What any rule of HQ's says, but for one that says every property (`all`).
  const names = [...new Set(rules.filter((rule) => !rule.style.getPropertyValue('all')).flatMap((rule) => [...rule.style]))].filter((name) => !name.startsWith('--'));
  const pageSheet = [...document.styleSheets].find((sheet) => sheet.href && !app.includes(sheet));
  const still = pageSheet.insertRule('*, ::before, ::after { transition: none !important; }', pageSheet.cssRules.length);
  const read = () => els.map((el) => Object.fromEntries(['', '::before', '::after'].map((pseudo) => {
    const style = getComputedStyle(el, pseudo || null);
    return [pseudo, pseudo && style.content === 'none' ? ['none'] : names.map((name) => style.getPropertyValue(name))];
  })));
  const before = read();
  const written = rules.map((rule) => rule.selectorText);
  const unwritten = [];
  rules.forEach((rule, i) => {
    rule.selectorText = split(written[i]).map((part) => { const [base, pseudo] = subject(part); return `${base}:not(${root} *)${pseudo}`; }).join(', ');
    if (rule.selectorText === written[i]) unwritten.push(written[i]);
  });
  const after = read();
  rules.forEach((rule, i) => { rule.selectorText = written[i]; });
  pageSheet.deleteRule(still);
  const reached = new Set();
  els.forEach((el, i) => {
    for (const [pseudo, values] of Object.entries(before[i])) {
      const now = after[i][pseudo];
      if (values.length !== now.length) {
        reached.add(`${describe(el)}${pseudo} content: drawn by HQ's rules alone`);
        continue;
      }
      values.forEach((value, j) => {
        if (value === now[j]) return;
        const matched = rules.filter((rule) => matches(rule, el, pseudo));
        const saying = matched.filter((rule) => rule.style.getPropertyValue(names[j]) !== '');
        const by = (saying.length ? saying : matched).map((rule) => rule.selectorText).join(' | ');
        reached.add(`${describe(el)}${pseudo} ${names[j]}: ${value} (HQ's), ${now[j]} (the viewer's), by ${by}`);
      });
    }
  });
  return {elements: els.length, unwritten, reached: [...reached].slice(0, 40)};
}"""
)

_REFERENCE_OPEN_MENU = "(root) => document.querySelector(root).querySelector('header button').click()"


def _strings(value):
    """Every string ``value`` holds, at any depth."""

    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return set().union(*map(_strings, value)) if value else set()
    return set()


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
        """The groups the document declares, each with its own operations, then
        its domains, in its order, and nothing else: none for the schemas."""

        outline = reference_outline(self.document)
        self.assertTrue(any(under for _, _, under in outline))
        self.assertTrue(any(own and under for _, own, under in outline))
        self.visit(WIDE)
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

    def test_no_engine_refuses_a_style_the_viewer_writes(self):
        """In each engine a browser leg draws in, the reference drawn whole in
        both themes is refused nothing by its policy, which admits the
        <style> elements the bundle writes without a nonce."""

        own = type(self).browser
        for engine in GATE_ENGINES:
            self.browser = own if engine == own.browser_type.name else getattr(self.playwright, engine).launch()
            # Closed once the subtest is over, so a failure is shot first.
            try:
                with self.subTest(engine=engine):
                    self.start()
                    for name in (REFERENCE, REFERENCE_DARK):
                        self.visit(WIDE, name)
                        self.assertEqual(draw_whole_reference(self.page), 0)
                    self.assertEqual(self.refusals_so_far(), [])
            finally:
                self.close_context()
                self.context = None
                if self.browser is not own:
                    self.browser.close()
                self.browser = own

    def assert_read_whole(self):
        """No text the viewer draws is cut short, marked with an ellipsis or
        not, nothing scrolls sideways, and a path starts a new line only at a
        "/", "." or "-" (or a query's mark), in each palette."""

        for palette in ("light", "dark"):
            with self.subTest(palette=palette):
                self.in_scheme(palette)
                self.settle()
                cut, sideways, broken = self.read(
                    [
                        (_REFERENCE_CUTS, [SELECTORS["reference"], SELECTORS["unseen"]]),
                        (_REFERENCE_SIDEWAYS, SELECTORS["reference"]),
                        (_REFERENCE_PATH_BREAKS, [SELECTORS["reference"], SELECTORS["unseen"]]),
                    ]
                )
                self.assertEqual(cut, [])
                self.assertEqual(sideways, [])
                self.assertEqual(broken, [])

    def assert_fields_read_by_type(self):
        """A field, a body and an answer say their type ("string", "object"),
        never the name of the schema they refer to: the schemas are not drawn
        on their own, so a name would point at nothing a reader can open. A
        name the document also says as text (an operation's title) is that
        text."""

        said = _strings(self.document["paths"]) | _strings(self.document["tags"])
        named = set(self.document["components"]["schemas"]) - said
        drawn = set(self.page.evaluate(_REFERENCE_WORDS, SELECTORS["reference"]))
        self.assertTrue(drawn)
        self.assertEqual(sorted(named & drawn), [])

    def assert_drawn_by_the_viewer_alone(self):
        """No rule of HQ's stylesheet reaches an element the viewer draws (an
        element rule, or a class of HQ's whose word the viewer also uses): with
        every one of them held to the page outside the viewer, every element
        and every box drawn before or after one is drawn as it was."""

        sheets = [static(sheet) for sheet in SHEETS]
        reached = self.page.evaluate(_REFERENCE_REACHED, [SELECTORS["reference"], sheets])
        self.assertGreater(reached["elements"], 100)
        self.assertEqual(reached["unwritten"], [])
        self.assertEqual(reached["reached"], [])

    def test_the_reference_opened_whole_reads_whole(self):
        """The reference opened once, every tag as a reader opens it, read at
        each width: whole in both palettes, at the widest each field by its
        type and the viewer's markup drawn by its own rules alone."""

        opened = every_tag(self.document)
        self.visit(WIDTHS[0])
        for width in WIDTHS:
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                # What a width draws that the last did not is opened too; what
                # is open already stays open.
                undrawn = open_tag(self.page, opened, step=lambda each: self.subTest(opening=each))
                self.assertEqual(undrawn, [])
                self.assert_read_whole()
                if width == WIDE:
                    with self.subTest(check="the viewer's markup is drawn by its own rules alone"):
                        self.assert_drawn_by_the_viewer_alone()
                    with self.subTest(check="a field reads by its type"):
                        self.assert_fields_read_by_type()

    def test_a_path_starts_a_line_only_at_a_mark(self):
        """A hyphenated path starts each line at a mark, never after a hyphen;
        a digest wider than its line starts one at its mark, then runs on
        where it meets the edge. Each is copied as written."""

        paths = [_HYPHENATED_PATH, _DIGEST_PATH]
        for width in (REFERENCE_PHONE, WIDE):
            with self.subTest(width=width):
                self.visit(width)
                self.assertEqual(open_tag(self.page, opened_walk(self.document)[0]), [])
                self.page.locator(SELECTORS["reference_sample"]).first.wait_for(state="attached")
                planted = self.page.evaluate(_PLANT_PATHS, [SELECTORS["reference_sample"], paths])
                self.assertEqual([path["text"] for path in planted], paths)
                for path in planted:
                    self.assertGreater(path["lines"], 1)
                broken = self.page.evaluate(_REFERENCE_PATH_BREAKS, [SELECTORS["reference"], SELECTORS["unseen"]])
                # Only the planted paths: what the document's own examples
                # draw is the read-whole test's.
                self.assertEqual([lines for lines in broken if lines.replace(" | ", "") in paths], [])

    def groups(self):
        """Each tag that holds only other tags, with the slugs of those tags."""

        own = {operation["tags"][0] for item in self.document["paths"].values() for operation in item.values()}
        under: dict[str, list[str]] = {}
        for tag in self.document["tags"]:
            if "parent" in tag:
                under.setdefault(tag["parent"], []).append(scalar_slug(tag["name"]))
        return {name: held for name, held in under.items() if name not in own}

    def after_a_group(self):
        """The address of the first tag outside any group that follows one."""

        groups = self.groups()
        tags = [tag["name"] for tag in self.document["tags"] if "parent" not in tag]
        following = next(later for earlier, later in itertools.pairwise(tags) if earlier in groups)
        return f"#tag/{scalar_slug(following)}"

    def test_a_group_links_each_domain_it_holds(self):
        """A tag that only groups others reads as their index wherever the page
        lands, closed or open: a link to each domain's section under its
        heading, never a heading alone."""

        groups = self.groups()
        self.assertTrue(groups)
        last = next(tag["name"] for tag in reversed(self.document["tags"]) if "parent" not in tag)
        self.visit(WIDE, fragment=f"#tag/{scalar_slug(last)}")
        draw_whole_reference(self.page)
        for group, held in groups.items():
            with self.subTest(group=group):
                links = self.page.evaluate(_SECTION_LINKS, [SELECTORS["reference"], f"/tag/{scalar_slug(group)}"])
                self.assertEqual(links, [f"#tag/{slug}" for slug in held])

    def test_a_link_in_a_groups_index_lands_on_its_domain(self):
        """Following a domain's link in its group's index puts that domain's
        section at the top of the view, and the sidebar marks it."""

        summary = {tag["name"]: tag["summary"] for tag in self.document["tags"]}
        group, held = next(iter(self.groups().items()))
        domain = next(tag for tag in self.document["tags"] if scalar_slug(tag["name"]) == held[0])
        address = f"#tag/{held[0]}"
        self.visit(WIDE)
        content = self.page.locator(SELECTORS["reference"]).get_by_role("main")
        section = content.get_by_role("region", name=summary[group]).first
        section.get_by_role("link", name=domain["summary"], exact=True).first.click()
        self.page.wait_for_function("(address) => location.hash === address", arg=address)
        self.page.wait_for_function(_REFERENCE_HEADING, arg=address, polling=100)
        self.settle()
        place = self.page.evaluate(_REFERENCE_PLACE, SELECTORS["reference"])
        self.assertTrue(place["under"].endswith(f"/{address[1:]}"), place)
        self.assertEqual(place["current"], address)

    def test_a_path_breaks_only_in_a_list_with_the_row_to_itself(self):
        """A tag's list of operations sits beside its description only where
        each of its paths fits on one line there, and every list is one width,
        so their edges line up from section to section."""

        for width in (REFERENCE_PHONE, _LAPTOP_WINDOW, WIDE):
            with self.subTest(width=width):
                self.visit(width)
                draw_whole_reference(self.page)
                self.assertEqual(self.page.evaluate(_PATHS_SHARING_A_ROW, SELECTORS["reference"]), [])
                edges = self.page.evaluate(_LIST_EDGES, SELECTORS["reference"])
                self.assertGreater(len(edges), 1)
                self.assertEqual(len(set(edges)), 1, edges)

    def test_the_entry_named_is_the_one_under_the_bars(self):
        """Landing on a tag shows it from its top, nothing of the one before it,
        and as the reader scrolls either way, the entry the reference names (the
        sidebar's current item, the bar's name where the sidebar folds away) is
        the one drawn just under the bars over the page."""

        address = self.after_a_group()
        for width in (_LAPTOP_WINDOW, WIDE):
            with self.subTest(width=width):
                self.visit(width, fragment=address)
                self.page.wait_for_function(_REFERENCE_HEADING, arg=address, polling=100)
                self.settle()
                place = self.page.evaluate(_REFERENCE_PLACE, SELECTORS["reference"])
                self.assertTrue(place["under"].endswith(f"/{address[1:]}"), place)
                self.assertAlmostEqual(place["top"], place["line"], delta=1)
                for step in (-0.3, -0.3, -0.3, 0.5, 0.5):
                    self.page.evaluate("(share) => window.scrollBy(0, innerHeight * share)", step)
                    self.settle()
                    place = self.page.evaluate(_REFERENCE_PLACE, SELECTORS["reference"])
                    with self.subTest(step=step, under=place["under"]):
                        self.assertTrue(place["current"])
                        self.assertTrue(place["under"].endswith(f"/{place['current'][1:]}"), place)

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
