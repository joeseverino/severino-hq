"""What each page costs to draw, rendered in this process on its own database: the queries it
makes, the records its tables hold and the HTML it sends.

`manage.py page_costs` walks it for perf and for a release's rehearsal on a copy of production's
data (controller/host/rehearse.go), so the numbers come from the measured release's own code. A
repeat of a statement is a query the page runs again for another row (N+1), so
`repeated_queries` grows with the rows a page draws.

The walk signs in as the first active operator through a session of its own, which it deletes
when it ends and never prints, and asks as a browser does through HQ's proxy: over HTTPS, at the
site's own host.
"""

from collections import Counter
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any, NamedTuple, override
from urllib.parse import urljoin

lazy from django.conf import settings
lazy from django.contrib.auth import get_user_model
lazy from django.db import connection
lazy from django.test import Client
lazy from django.urls import reverse

lazy from hq.platform.application.entity_links import link_url
lazy from hq.platform.application.pages import listed_routes
lazy from hq.platform.application.request_identity import signed_in
lazy from hq.platform.application.roles import Role
lazy from hq.platform.core.dev_sign_in import new_session

# Why a walk measured nothing, in the document it writes.
NO_OPERATOR = "no active operator to sign in as"
SIGNED_OUT = "the walk was answered signed out"
# More than the longest chain of redirects within HQ a page takes.
_HOPS = 5
# Elements that never hold others, so never close.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
# A table body's rows that are no record: a group's heading, and the line held beneath an open row.
_NOT_RECORDS = frozenset({"row-group", "row-detail"})
# A group's heading is drawn once for many records, so its markup is the page's, not a record's.
_GROUPING = frozenset({"row-group"})


def listed_pages() -> list[str]:
    """The address of each page a reader finds by the navigation or the header."""

    return list(dict.fromkeys(reverse(route) for route in listed_routes()))


def walk(pages: list[str]) -> dict[str, Any]:
    """The document `page_costs` prints: the release's revision, and each page's costs by its
    address, or why none was measured."""

    said: dict[str, Any] = {"revision": settings.SEVERINO_HQ_REVISION, "pages": {}}
    operator = (
        get_user_model()._default_manager.filter(person_role__role=Role.OPERATOR, is_active=True).order_by("pk").first()
    )
    if operator is None:
        return said | {"unread": NO_OPERATOR}
    session = new_session(operator)
    client = Client(HTTP_HOST=settings.SEVERINO_SITE_HOST, raise_request_exception=False)
    client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    try:
        said["pages"] = {page: cost(client, page) for page in pages}
    except _SignedOut:
        return said | {"unread": SIGNED_OUT}
    finally:
        session.delete()
    return said


class _SignedOut(Exception):
    """A page answered someone other than the operator the walk signed in as, so its costs would
    be a signed-out reader's."""


def cost(client: Client, page: str) -> dict[str, int]:
    """What ``page`` costs, asked once first so what a first request caches is not its cost."""

    _answer(client, page)
    sent: Counter[str] = Counter()

    def record(execute, sql, params, many, context):
        sent[sql] += 1
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        response = _answer(client, page)
        body = response.getvalue()
    if signed_in(response.wsgi_request) is None:
        raise _SignedOut(page)
    rows = data_rows(body.decode("utf-8", errors="replace"))
    total = sum(sent.values())
    return {
        "status": response.status_code,
        "queries": total,
        "repeated_queries": total - len(sent),
        "rows": rows.count,
        "html_bytes": len(body),
        "row_html_bytes": rows.html_bytes,
    }


# What a page costs once, apart from what it costs per record it draws, read from what ``cost``
# measured, so a page that drew more records reads as more rows, never as a heavier page.


def page_queries(measured: Mapping[str, int]) -> int:
    """The first of each statement it sent; every repeat is its ``repeated_queries``."""

    return measured["queries"] - measured["repeated_queries"]


def page_bytes(measured: Mapping[str, int]) -> int:
    """Its HTML outside its records' rows."""

    return measured["html_bytes"] - measured["row_html_bytes"]


def bytes_per_row(measured: Mapping[str, int]) -> float | None:
    """The HTML each record's rows take, on average; None where it drew none."""

    return measured["row_html_bytes"] / measured["rows"] if measured.get("rows") else None


def _answer(client: Client, page: str):
    """The answer ``page`` lands on, each redirect within HQ followed; a redirect out of HQ is
    the answer, which the test client would otherwise ask of HQ under another host, as would a
    redirect to HQ's own address."""

    response = client.get(page, secure=True)
    for _ in range(_HOPS):
        if response.status_code // 100 != 3 or "Location" not in response:
            break
        # A path is a page of HQ's; an address naming a host leaves it.
        if not link_url(page := urljoin(page, response["Location"])).startswith("/"):
            break
        response = client.get(page, secure=True)
    return response


class Rows(NamedTuple):
    """The records a page's tables hold, and the bytes of HTML their rows take, each record's
    held-back line included: what the page spends per record, apart from what it spends once."""

    count: int
    html_bytes: int


class _Rows(HTMLParser):
    """Counts the records a reader meets in a page's tables, as the browser gates select them
    (`main .data-table > tbody > tr`), leaving out the rows that hold no record of their own, and
    measures the HTML of every row a record draws."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.html = html
        self.line_starts = [0, *(index + 1 for index, char in enumerate(html) if char == "\n")]
        self.open: list[tuple[str, frozenset[str]]] = []
        self.rows = 0
        # The depth in ``open`` of the row being measured, and where in the text it starts.
        self.measuring: tuple[int, int] | None = None
        self.row_bytes = 0

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = frozenset((dict(attrs).get("class") or "").split())
        if tag == "tr" and self._in_body():
            self.rows += 0 if classes & _NOT_RECORDS else 1
            if not classes & _GROUPING:
                self.measuring = (len(self.open), self._offset())
        if tag not in _VOID:
            self.open.append((tag, classes))

    def _in_body(self) -> bool:
        return (
            len(self.open) >= 2
            and self.open[-1][0] == "tbody"
            and self.open[-2][0] == "table"
            and "data-table" in self.open[-2][1]
            and any(tag == "main" for tag, _ in self.open)
        )

    @override
    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.open) - 1, -1, -1):
            if self.open[depth][0] == tag:
                del self.open[depth:]
                if self.measuring and depth <= self.measuring[0]:
                    end = self.html.find(">", self._offset()) + 1 or len(self.html)
                    self.row_bytes += len(self.html[self.measuring[1] : end].encode())
                    self.measuring = None
                return


def data_rows(html: str) -> Rows:
    """The records the tables of a page's main region hold, and the HTML their rows take."""

    reader = _Rows(html)
    reader.feed(html)
    reader.close()
    return Rows(reader.rows, reader.row_bytes)
