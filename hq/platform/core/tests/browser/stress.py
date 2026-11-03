"""The stress estate: every page it draws."""

from functools import cache
from types import MappingProxyType

from django.db import transaction

from hq.platform.core import seeds
from hq.platform.core.tests.test_browser_dense_fixtures import DENSE_PAGES, _asked_on_opening
from hq.platform.core.tests.test_browser_derived_fixtures import derived_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, render_pages

# Every page, from the sparse estate and the dense one. The overflow and
# density checks run all of them at WIDTHS.
ALL_PAGES = (*PAGES, *DENSE_PAGES)


def stress_pages(asked):
    """The pages of the three stress estates that ``asked`` names, by name:
    an estate is built only for a page of its own asked, and rolled back once
    rendered, so nothing else sees it. A sparse or
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
    return pages


@cache
def stress_estate():
    """Every page of the three stress estates and the derived lists' names:
    ``(pages, lists)``. Rendered once a process, so a worker handed a second
    layout class does not build the estates again."""

    lists = tuple(derived_pages())
    return MappingProxyType(stress_pages([*ALL_PAGES, *lists])), lists
