"""dev:look's walk of the API reference, a vendored viewer HQ serves: it
opens the reference on request, each tag or one operation, every entry drawn."""

import difflib
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client
from django.urls import reverse

from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.tests.browser.gate import answered
from hq.platform.core.tests.browser.selectors import SELECTORS, css

# The API reference: a vendored viewer mounted inside HQ's frame. Its markup is
# the vendor's, so what reads it finds it by tag and ARIA, never by class.
REFERENCE = "api/docs"

REFERENCE_DARK = "api/docs/dark"

_REFERENCE_MOUNTED = "() => document.querySelector('main main h1') !== null"


def wait_for_reference(page):
    """Until the viewer has drawn the reference."""

    page.wait_for_function(_REFERENCE_MOUNTED)


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

    return page.evaluate(_DRAW_WHOLE, css(page, SELECTORS["reference"]))


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

    return page.evaluate(_SERVED, css(page, SELECTORS["reference"]))


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

    arg = css(page, [SELECTORS["reference"], SHOW_MORE, list(opened.tags), list(opened.parts), list(opened.shared)])
    for _round in range(20):
        draw_whole_reference(page)
        if not page.evaluate(_OPEN_ROUND, arg):
            break
    draw_whole_reference(page)
    return page.evaluate(_UNDRAWN, css(page, [SELECTORS["reference"], list(opened.tags), list(opened.parts)]))


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
