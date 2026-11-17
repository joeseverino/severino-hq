"""The component catalog: each shared part filled the way an extension's page
fills it, which no host page does, drawn through its own partial or tag on a
page of its own and held by the whole survey. A new state of a shared part is
one entry in the module of its family under ``families/``, the prefix of its
name (``part/``, ``table/``). A new family is a new module there and one
import below."""

import re
from dataclasses import replace

from django.template.loader import render_to_string
from django.utils.html import escape

from hq.platform.application.ui import PageNavigation, PageSection
from hq.platform.core.template_contract import parts
from hq.platform.core.template_parts import Part
from hq.platform.core.templatetags.stylesheet_tags import stylesheets
from hq.platform.core.tests.browser.catalog.common import Entry, head_of, part_page
from hq.platform.core.tests.browser.catalog.families import chart, form, graph, part, status, table


def default_name(each: Part) -> str:
    """The name of a part's default entry: ``part/`` and its tag, or its
    partial's name when it has none."""

    stem = each.name or each.partial.rpartition("/")[2].removesuffix(".html").lstrip("_")
    return f"part/{stem.replace('_', '-')}"


def _assembled(*families) -> dict[str, Entry]:
    """Every family's ``ENTRIES``, each in its module's own order, after each
    part's default entry: its ``Part.example`` as a page writes it, given what
    the entry of its name with no template gives it, in any family. A family
    holds only names it begins, but for those, and never writes a default's
    template, so no two modules name one entry and every part has its own."""

    named = {default_name(each): each for each in parts()}
    given = {name: entry for family in families for name, entry in family.ENTRIES.items() if not entry.template}
    if strays := sorted(given.keys() - named.keys()):
        raise ImportError(f"an entry with no template is a part's default, but no part is named {strays}")
    catalog = {name: replace(given.get(name, Entry()), template=each.example) for name, each in sorted(named.items())}
    for family in families:
        prefix = family.__name__.rpartition(".")[2]
        drawn = {name: entry for name, entry in family.ENTRIES.items() if entry.template}
        if strays := [name for name in drawn if not name.startswith(f"{prefix}/")]:
            raise ImportError(f"families/{prefix}.py holds entries of another family: {strays}")
        if restated := sorted(drawn.keys() & named.keys()):
            raise ImportError(f"families/{prefix}.py writes a part's default entry, drawn from its example: {restated}")
        catalog |= drawn
    return catalog


CATALOG = _assembled(chart, form, graph, part, status, table)

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
    keeps its autofocus (``AUTOFOCUS``)."""

    drawn: list[str] = []
    families: list[str] = []
    sheets = stylesheets()
    for name, entry in CATALOG.items():
        if entry.frame:
            continue
        anchor = ""
        if (family := name.partition("/")[0]) not in families:
            families.append(family)
            anchor = f' id="design-{family}"'
        heading = render_to_string(NAME, {"heading": escape(name)})
        part = COLUMN.format(AUTOFOCUS.sub(r"\1", entry.part(name)))
        drawn.append(
            f'<div{anchor} role="region" aria-label="{escape(name)}"><div class="specimen-name">{heading}</div>'
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

    return _gallery_page() if name == GALLERY else CATALOG[name].page(name)


def left_out_on(name) -> tuple[str, ...]:
    """The axe rules the page ``name`` of ``PAGES`` leaves out."""

    return tuple(GALLERY_LEAVES_OUT) if name == GALLERY else ()
