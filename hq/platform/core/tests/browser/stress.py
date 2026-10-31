"""The stress estate: every page it draws, and the parts it is built to show."""

from functools import cache
from types import MappingProxyType

from django.db import transaction

from hq.platform.core import seeds
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES, _asked_on_opening
from hq.platform.core.tests.test_browser_derived_fixtures import derived_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, render_pages
lazy from hq.platform.core.templatetags.stylesheet_tags import stylesheets

# Every page, from the sparse estate and the dense one. The overflow and
# density checks run all of them at WIDTHS.
ALL_PAGES = (*PAGES, *DENSE_PAGES)


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


def part_page(markup):
    """A page of HQ's stylesheets with ``markup`` alone in its main."""

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{stylesheets()}</head>"
        f'<body><main class="content">{markup}</main></body></html>'
    )


def shared_parts():
    return {name: part_page(markup) for name, markup in SHARED_PARTS.items()}


def stress_pages(asked):
    """The pages of the three stress estates and the probes beside them that
    ``asked`` names, by name: an estate is built only for a page of its own
    asked, and rolled back once rendered, so nothing else sees it. A sparse or
    dense page asks for its readings on opening, and hears what the dense
    estate answers, so that one is built for either."""

    asked = set(asked)
    sparse, dense = ({name: page for name, page in family.items() if name in asked} for family in (PAGES, DENSE_PAGES))
    derived = {name: page for name, page in derived_pages().items() if name in asked}
    pages: dict[str, str] = {}
    for seed, chosen, needed in (
        ("sparse", sparse, sparse),
        ("dense", dense, sparse or dense),
        ("derived", derived, derived),
    ):
        if needed:
            with transaction.atomic(), seeds.built(seed) as owner:
                pages |= render_pages(owner, chosen)
                # Asked once every page is drawn, so each is drawn before anything was asked.
                pages |= _asked_on_opening(owner) if seed == "dense" else {}
                transaction.set_rollback(True)
    probes = shared_parts() if asked & set(SHARED_PARTS) else {}
    return pages | {name: page for name, page in probes.items() if name in asked}


@cache
def stress_estate():
    """Every page of the three stress estates, the derived lists' names, and
    the probes beside them: ``(pages, lists)``. Rendered once a process, so a
    worker handed a second layout class does not build the estates again."""

    lists = tuple(derived_pages())
    return MappingProxyType(stress_pages([*ALL_PAGES, *lists, *SHARED_PARTS])), lists
