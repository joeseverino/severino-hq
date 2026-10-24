"""The stress estate: every page it draws, and the parts and heads it is built to show."""

from functools import cache
from types import MappingProxyType

from django.db import transaction
lazy from django.template.loader import render_to_string

from hq.platform.core import seeds
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES, render_dense_pages
from hq.platform.core.tests.test_browser_derived_fixtures import render_derived_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, render_pages
lazy from hq.platform.application.asks import Ask
lazy from hq.platform.application.pages import Page, PageAction
lazy from hq.platform.core.templatetags.stylesheet_tags import stylesheets

# Every page, from the sparse estate and the dense one. The overflow and
# density checks run all of them at WIDTHS.
ALL_PAGES = (*PAGES, *DENSE_PAGES)

# The pages that draw a route, sparse and dense.
ROUTE_PAGES = ("service", "connections", "dense/service", "dense/connections")

# Heads whose lead control is one that says what it is doing beside itself,
# drawn by the head's own partial: a one-word title, two words, and a name.
SPEAKING_HEADS = {
    "head/word": "Reports",
    "head/words": "Two words",
    "head/name": "example-host.example.com",
}

# A shared component filled the way an extension's page fills it, which no host
# page does: a section's heading with a long name and states beside it.
SHARED_PARTS = {
    "part/section-head": (
        '<section class="card"><div class="section-head">'
        "<h2>An example heading long enough to need the row</h2>"
        '<span class="pill pill-good">Example state</span>'
        '<span class="pill pill-neutral">Another example state</span>'
        "</div><p>Example.</p></section>"
    ),
}


def shared_parts():
    return {
        name: (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"{stylesheets()}</head>"
            f'<body><main class="content">{markup}</main></body></html>'
        )
        for name, markup in SHARED_PARTS.items()
    }


def speaking_heads():
    actions = (
        Ask("Read everything", "/probe/", primary=True),
        PageAction("First other", "/probe/first/"),
        PageAction("Second other", "/probe/second/"),
    )
    return {
        name: (
            f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"{stylesheets()}</head><body><main>"
            + render_to_string("partials/_page_head.html", {"page": Page(title=title, actions=actions)})
            + "</main></body></html>"
        )
        for name, title in SPEAKING_HEADS.items()
    }


@cache
def stress_estate():
    """Every page of the three stress estates, the derived lists' names, and
    the probes beside them: ``(pages, lists)``. Rendered once a process, so a
    worker handed a second layout class does not build the estates again;
    each estate is rolled back once rendered, so nothing else sees it."""

    with transaction.atomic(), seeds.built("sparse") as owner:
        pages = render_pages(owner)
        transaction.set_rollback(True)
    with transaction.atomic(), seeds.built("dense") as owner:
        pages |= render_dense_pages(owner)
        transaction.set_rollback(True)
    with transaction.atomic(), seeds.built("derived") as owner:
        lists = render_derived_pages(owner)
        transaction.set_rollback(True)
    return MappingProxyType(pages | lists | speaking_heads() | shared_parts()), tuple(lists)
