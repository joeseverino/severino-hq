"""What each page cost in the rehearsal of each of HQ's live releases: the
release production runs, as the controller last read it, and the one before
it, as its snapshot kept them."""

from hq.domains.control_plane.observations.host import REHEARSAL_KIND, RehearsedPage
from hq.platform.core.page_costs import listed_pages

from .names import HQ_REVISION, hexed
from .sweep import store

# The pages whose costs moved between the two live releases: one draws a
# query per row more, one sends heavier rows, one a lighter page.
MOVED = 3


def _drawn(page: str, moved: int | None) -> dict:
    """A page's costs, the same for every release but where ``moved`` names
    which of its costs this release changed."""

    seed = int(hexed("page", page, length=8), 16)
    rows = seed % 40
    row_bytes = rows * (300 + seed % 500) + (rows * 120 if moved == 1 else 0)
    return {
        "page": page,
        "status": 302 if page == "/" else 200,
        "queries": 8 + seed % 30 + (rows if moved == 0 else 0),
        "repeated_queries": seed % 5 + (rows if moved == 0 else 0),
        "rows": rows,
        "html_bytes": 30_000 + seed % 40_000 + row_bytes - (6_000 if moved == 2 else 0),
        "row_html_bytes": row_bytes,
    }


def walked() -> None:
    """The rehearsal of the release production runs, as the controller read
    it, before the sweep that keeps the release's snapshot. It moved the first
    few pages' costs and drew the last page, which the release before it did
    not have."""

    pages = listed_pages()
    store(
        REHEARSAL_KIND,
        {"revision": HQ_REVISION, "pages": [_drawn(page, at if at < MOVED else None) for at, page in enumerate(pages)]},
    )


def before() -> tuple[RehearsedPage, ...]:
    """The release before it, as its rehearsal drew every page but the last."""

    return tuple(RehearsedPage.model_validate(_drawn(page, None)) for page in listed_pages()[:-1])
