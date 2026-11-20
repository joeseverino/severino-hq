"""The stress estate: every page of the crowded estate and the derived lists."""

from functools import cache
from types import MappingProxyType

from hq.platform.core.tests.browser.estate import owner_client
from hq.platform.core.tests.test_browser_derived_fixtures import derived_pages
from hq.platform.core.tests.test_browser_fixtures import PAGES, asked_on_opening, render_pages


def stress_pages(asked):
    """The pages of the crowded estate and the derived lists that ``asked``
    names, by name: a set is built only for a page of its own asked, and
    rolled back once rendered, so nothing else sees it. A crowded page asks
    for its readings on opening, and hears what that estate answers."""

    asked = set(asked)
    pages: dict[str, str] = {}
    for seed, family in (("crowded", PAGES), ("derived", derived_pages())):
        chosen = {name: page for name, page in family.items() if name in asked}
        if chosen:
            with owner_client(seed) as client:
                pages |= render_pages(client, chosen)
                # Asked once every page is drawn, so each is drawn before anything was asked.
                pages |= asked_on_opening(client) if seed == "crowded" else {}
    return pages


@cache
def stress_estate():
    """Every page of the crowded estate and the derived lists, and the derived
    lists' names: ``(pages, lists)``. Rendered once a process, so a worker
    handed a second layout class does not build the estates again."""

    lists = tuple(derived_pages())
    return MappingProxyType(stress_pages([*PAGES, *lists])), lists
