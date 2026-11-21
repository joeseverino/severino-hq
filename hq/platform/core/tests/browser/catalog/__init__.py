"""The component catalog: each shared part filled the way an extension's page
fills it, which no host page does, drawn through its own partial or tag on a
page of its own and held by the whole survey. Each part's examples are its
entries: its ``example`` and each of its ``states``, declared on its ``Part``.
A family module under ``families/`` holds what a part's examples do not, the
entries that draw several parts together, each named by its family's prefix
(``table/``, ``form/``); a new family is a new module there and one import
below."""

import functools
import re
from dataclasses import replace

from django.template.loader import render_to_string
from django.utils.html import escape

from hq.platform.application.ui import PageNavigation, PageSection
from hq.platform.core.template_contract import parts
from hq.platform.core.template_parts import Example, Part
from hq.platform.core.templatetags.stylesheet_tags import stylesheets
from hq.platform.core.tests.browser.catalog.common import alone, head_of, page as entry_page, part_page
from hq.platform.core.tests.browser.catalog.families import chart, form, graph, status, table, value


def default_name(each: Part) -> str:
    """The name of a part's default entry: ``part/`` and its tag, or its
    partial's name when it has none."""

    stem = each.name or each.partial.rpartition("/")[2].removesuffix(".html").lstrip("_")
    return f"part/{stem.replace('_', '-')}"


@functools.cache
def _defaults() -> dict[str, Part]:
    """Every part by the name of its default entry."""

    return {default_name(each): each for each in parts()}


def _assembled(*families) -> dict[str, Example]:
    """Each part's default under its name and each of its states under the
    default's name and the state's, then every family's ``ENTRIES``, each in
    its module's own order, every name its family's prefix."""

    catalog: dict[str, Example] = {}
    for name, each in sorted(_defaults().items()):
        catalog[name] = each.default()
        catalog |= {f"{name}-{state}": example for state, example in each.states.items()}
    for family in families:
        prefix = family.__name__.rpartition(".")[2]
        if strays := [name for name in family.ENTRIES if not name.startswith(f"{prefix}/")]:
            raise ImportError(f"families/{prefix}.py holds entries of another family: {strays}")
        catalog |= family.ENTRIES
    # What an example is given, built once, as the catalog is drawn.
    return {name: replace(entry, context=entry.given()) for name, entry in catalog.items()}


CATALOG = _assembled(chart, form, graph, status, table, value)

# Every entry on one page (`gallery_body`), held by the survey as each entry
# is (`layout.CatalogLayoutTests`), and the pages the catalog draws.
GALLERY = "gallery"
PAGES = (*CATALOG, GALLERY)
# The axe rules the gallery leaves out, with why, held everywhere else.
GALLERY_LEAVES_OUT = {
    "landmark-unique": "each entry is held to it on its own page; the gallery repeats parts by design",
}
# The gallery's title, as a page names itself.
GALLERY_TITLE = "Shared parts"
# The map of the gallery's families, as a page draws its own.
NAVIGATION = "partials/_page_navigation.html"
# An entry's name over it, as a section heads itself.
NAME = "partials/_section_head.html"
# The column an entry is drawn in on a page of its own.
COLUMN = '<div class="content">{}</div>'
# A control's autofocus, as a tag draws it. A part takes the focus of the page
# it stands on; in the gallery that page is every part's, which the first
# such part would scroll to on load, so the gallery draws none.
AUTOFOCUS = re.compile(r"(<[a-zA-Z][^<>]*?)\s+autofocus(?:=(?:\"[^\"]*\"|'[^']*'|[^\s>]*))?(?=[\s/>])")
# A link of a part to an address of HQ's, as a tag draws it. An example's
# addresses are made up, so in the gallery each such link points at the
# example's own region on the page, which every HQ has.
OWN_LINK = re.compile(r'(<a\b[^<>]*?\shref=")/(?!/)[^"]*(")')
# A start tag, and where its form would send a press. An example shows a
# part and never acts, and the page's scripts, which would answer a press,
# never reach a shadow root, so in the gallery each form loses its address
# and method and submits as `method="dialog"`, which outside a dialog does
# nothing and inside one closes it.
TAG = re.compile(r"<[a-zA-Z][^<>]*>")
SENDS = re.compile(r"\s(?:action|formaction|method|formmethod)=(?:\"[^\"]*\"|'[^']*'|[^\s>]*)")
FORM = re.compile(r"^<form\b", re.IGNORECASE)


def _inert(tag: re.Match) -> str:
    kept = SENDS.sub("", tag.group(0))
    return FORM.sub('<form method="dialog"', kept)


def gallery_body() -> str:
    """Every entry but the frame's own, in the catalog's order, each in a
    region named for it under its name, after a map that opens each family at
    its first entry: the Design page's content.

    Each part is drawn in a shadow root of its own, written in the markup
    (`<template shadowrootmode>`), with HQ's sheets and the column it stands in
    on its own page. Its ids, and what points at them (a label, a popover, a
    dialog, a button's form), are the part's alone there, so no example meets
    another or the page around it, and the page's scripts, which read the
    document, never act on one. Focus does cross a shadow root, so no part
    keeps its autofocus (``AUTOFOCUS``), a link does leave the page, so
    each of HQ's addresses points at the entry's region (``OWN_LINK``), and
    a form would send a press, so none does (``SENDS``)."""

    drawn: list[str] = []
    families: list[str] = []
    sheets = stylesheets()
    for name, entry in CATALOG.items():
        if entry.frame:
            continue
        anchor = f"design-{name}"
        if (family := name.partition("/")[0]) not in families:
            families.append(family)
            anchor = f"design-{family}"
        heading = render_to_string(NAME, {"heading": escape(name)})
        markup = TAG.sub(_inert, OWN_LINK.sub(rf"\g<1>#{escape(anchor)}\g<2>", alone(entry, name)))
        part = COLUMN.format(AUTOFOCUS.sub(r"\1", markup))
        drawn.append(
            f'<div id="{escape(anchor)}" role="region" aria-label="{escape(name)}">'
            f'<div class="specimen-name">{heading}</div>'
            f'<div class="specimen"><template shadowrootmode="open">{sheets}{part}</template></div></div>'
        )
    navigation = PageNavigation(tuple(PageSection(f"design-{family}", family) for family in families), label="Families")
    return (
        f'<div class="design-gallery">{render_to_string(NAVIGATION, {"navigation": navigation})}{"".join(drawn)}</div>'
    )


def _gallery_page() -> str:
    """The gallery on a page of its own: the document the survey judges, as
    the Design page draws it. The frame's parts (the site header, the demo's
    foot) hold to a page's edge, so each is judged on its page alone and the
    gallery draws none of them."""

    return part_page(GALLERY_TITLE, head_of(GALLERY_TITLE) + gallery_body())


def page(name) -> str:
    """The page ``name`` of ``PAGES`` is drawn on."""

    return _gallery_page() if name == GALLERY else entry_page(CATALOG[name], name)


def left_out_on(name) -> tuple[str, ...]:
    """The axe rules the page ``name`` of ``PAGES`` leaves out."""

    return tuple(GALLERY_LEAVES_OUT) if name == GALLERY else ()


def part_of(name):
    """The shared part the page ``name`` of ``PAGES`` is the default entry
    of, drawn from its ``Part.example``, or None."""

    return _defaults().get(name)


def premise_of(name):
    """The probe of what the page ``name`` of ``PAGES`` must draw to stand
    for its state (`Entry.premise`), or None."""

    return CATALOG[name].premise if name in CATALOG else None
