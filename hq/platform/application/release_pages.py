"""What each page cost in a release's rehearsal on a copy of production's data,
beside the previous release's (``release_snapshots`` keeps both), for the
``releases`` resource and the release's page."""

from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Annotated, Any, TypedDict

from hq.platform.core.page_costs import bytes_per_row, page_bytes, page_queries

from .describe import Describe
from .entity_links import link_url
from .release_snapshots import RehearsedPage, ReleaseReadings
from .tables import Cell, DataTable, TableColumn
from .tone import Tone
from .ui import measured


class PageCostRecord(TypedDict):
    """What a page costs once, apart from what it costs per record it draws."""

    page_queries: Annotated[int, Describe("The first of each SQL statement it sent.")]
    row_queries: Annotated[int, Describe("Each repeat of a statement it had sent, which grows with its rows.")]
    bytes_per_row: Annotated[float | None, Describe("The HTML each record's rows take; null where it drew none.")]
    page_bytes: Annotated[int, Describe("Its HTML outside its records' rows, in bytes.")]


class PageRecord(PageCostRecord):
    """One page as the release's rehearsal drew it on a copy of production's data."""

    page: Annotated[str, Describe("The page's path.")]
    status: Annotated[int, Describe("The HTTP status it answered.")]
    rows: Annotated[int, Describe("The records its tables drew.")]
    html_bytes: Annotated[int, Describe("The bytes of HTML it answered.")]
    previous: Annotated[
        PageCostRecord | None, Describe("The previous release's, where its rehearsal drew the page; null else.")
    ]


def rows(taken: ReleaseReadings | None, before: ReleaseReadings | None) -> list[dict[str, Any]]:
    """Each page the release's rehearsal drew, beside the previous release's drawing of it."""

    if taken is None:
        return []
    earlier = {page.page: page for page in before.pages} if before is not None else {}
    return [
        {
            "page": page.page,
            "status": page.status,
            "rows": page.rows,
            "html_bytes": page.html_bytes,
            **_costs(page),
            "previous": _costs(earlier[page.page]) if page.page in earlier else None,
        }
        for page in taken.pages
    ]


def _costs(page: RehearsedPage) -> dict[str, Any]:
    drawn = page.model_dump()
    return {
        "page_queries": page_queries(drawn),
        "row_queries": page.repeated_queries,
        "bytes_per_row": bytes_per_row(drawn),
        "page_bytes": page_bytes(drawn),
    }


# Each measure a page is compared by, with how its figure is written: what it
# costs once, and per record. Its rows move with production's data, so they
# are shown alone.
COMPARED: tuple[tuple[str, str, Callable[[Any], str]], ...] = (
    ("Page q", "page_queries", lambda value: measured(value, "")),
    ("Row q", "row_queries", lambda value: measured(value, "")),
    ("B/row", "bytes_per_row", lambda value: measured(value, "", 0)),
    ("Page KB", "page_bytes", lambda value: measured(value / 1024, "", 1)),
)


def _compared(page: Mapping[str, Any], key: str, written: Callable[[Any], str]) -> Cell:
    """A page's figure, and the previous release's after it where that differs."""

    said = written(page[key]) if page[key] is not None else ""
    before = page["previous"]
    if before is None or before[key] is None or written(before[key]) == said:
        return Cell(said)
    return Cell(said, parts=(Cell(f"from {written(before[key])}", muted=True),))


def _answered(page: Mapping[str, Any]) -> Cell:
    """The status it answered, serious where the rehearsal fails a release for it."""

    status = page["status"]
    fails = status != HTTPStatus.OK and not HTTPStatus(status).is_redirection
    return Cell(str(status), tone=str(Tone.SERIOUS) if fails else "")


def table(release: Mapping[str, Any]) -> DataTable:
    """What each page cost in the release's rehearsal, beside the previous release's."""

    columns = (
        TableColumn(
            "Page", kind="identifier", cell=lambda page: Cell(page["page"], link_url(page["page"]), readout=True)
        ),
        *(
            TableColumn(
                heading, kind="numeric", cell=lambda page, key=key, written=written: _compared(page, key, written)
            )
            for heading, key, written in COMPARED
        ),
        TableColumn("Rows", kind="numeric", cell=lambda page: Cell(measured(page["rows"], ""))),
        TableColumn("Status", kind="status", cell=_answered),
    )
    return DataTable.of("Pages", columns, release["pages"])
