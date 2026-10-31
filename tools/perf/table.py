"""One report, or one beside its base, as the table `perf` prints, and its summary.

A row is a page at one width, the server's numbers on its first width's row, and
a total row sums each column over every page, so a change's whole effect reads
as one number per column. The summary is what a reader of the session log needs,
and the table it summarizes is kept beside it.
"""

from collections.abc import Callable
from typing import Any

from tools.perf.measure import past_spread

Report = dict[str, Any]
Read = Callable[[dict[str, Any]], float | None]

# The server's columns, read from a page's `server`: header, value, decimals.
SERVER: tuple[tuple[str, Read, int], ...] = (
    ("p50 ms", lambda server: server.get("p50_ms"), 1),
    ("p95 ms", lambda server: server.get("p95_ms"), 1),
    ("queries", lambda server: server.get("queries"), 0),
    ("rows", lambda server: server.get("rows"), 0),
    ("HTML KB", lambda server: _kilobytes(server["html_bytes"]), 1),
)
# Beside a base, the server time's round-by-round difference from the base's, and a mark past its spread.
PAIRED: tuple[tuple[str, Read, int], ...] = (
    ("moved ms", lambda server: server.get("moved_ms"), 2),
    ("spread ms", lambda server: server.get("spread_ms"), 2),
)
PAST = "moved"
# The browser's, read from a page's `browser[width]`.
BROWSER: tuple[tuple[str, Read, int], ...] = (
    ("first KB", lambda seen: _kilobytes(seen["first"]["bytes"]), 1),
    ("first req", lambda seen: seen["first"]["requests"], 0),
    ("first FCP ms", lambda seen: seen["first"]["fcp_ms"], 0),
    ("first LCP ms", lambda seen: seen["first"]["lcp_ms"], 0),
    ("repeat KB", lambda seen: _kilobytes(seen["repeat"]["bytes"]), 1),
    ("repeat req", lambda seen: seen["repeat"]["requests"], 0),
    ("repeat FCP ms", lambda seen: seen["repeat"]["fcp_ms"], 0),
    ("repeat LCP ms", lambda seen: seen["repeat"]["lcp_ms"], 0),
    ("script ms", lambda seen: seen["script_ms"], 1),
    ("long tasks", lambda seen: seen["long_tasks"], 0),
    ("CLS", lambda seen: seen["cls"], 3),
)
# Under the table, read from the whole report: label, value, decimals, unit.
SUMMARY: tuple[tuple[str, Read, int, str], ...] = (
    ("memory idle", lambda report: report["memory"]["idle_bytes"] / 1024**2, 1, "MB"),
    ("memory after a pass over every page", lambda report: report["memory"]["after_pass_bytes"] / 1024**2, 1, "MB"),
    ("cold start to /health/ready/, median", lambda report: report["cold_start"]["median_ms"] / 1000, 2, "s"),
)
# The pages a summary names without a base: the slowest.
SLOWEST = 3


def server_headers(*, compared: bool) -> list[str]:
    return [name for name, _, _ in (*SERVER, *(PAIRED if compared else ()))] + ([PAST] if compared else [])


def headers(*, compared: bool) -> list[str]:
    return ["page", "width", *server_headers(compared=compared), *(name for name, _, _ in BROWSER)]


def _kilobytes(size: float) -> float:
    return size / 1024


def _number(value: float | None, places: int) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _moved(value: float | None, base: float | None, places: int) -> str:
    """How far ``value`` moved from ``base``, signed; nothing when either is missing."""

    if value is None or base is None:
        return ""
    moved = round(value - base, places)
    return f"{moved:+.{places}f}" if moved else "0"


def cell(value: float | None, base: float | None, places: int, *, compared: bool) -> str:
    said = _number(value, places)
    return f"{said} {_moved(value, base, places)}".rstrip() if compared else said


def _pages(report: Report) -> dict[str, dict[str, Any]]:
    return {entry["page"]: entry for entry in report["pages"]}


def _widths(report: Report) -> list[str]:
    return [str(width) for width in report["widths"]]


def _read(read: Read, source: dict[str, Any] | None) -> float | None:
    if not source:
        return None
    try:
        return read(source)
    except KeyError, TypeError:
        return None


def _server(entry: dict[str, Any], base: dict[str, Any] | None, *, compared: bool) -> list[str]:
    """A page's server numbers, each beside how far it moved from ``base``'s when ``compared``."""

    server, before = entry.get("server"), (base or {}).get("server")
    cells = [cell(_read(read, server), _read(read, before), places, compared=compared) for _, read, places in SERVER]
    if compared:
        cells += [_number(_read(read, server), places) for _, read, places in PAIRED]
        cells.append(PAST if past_spread(server or {}) else "")
    return cells


# The server's numbers on a page's first width only: they do not vary by width.
def _row(entry: dict[str, Any], base: dict[str, Any] | None, width: str, *, first: bool, compared: bool) -> list[str]:
    server = _server(entry, base, compared=compared) if first else [""] * len(server_headers(compared=compared))
    base = base or {}
    seen, seen_before = entry.get("browser", {}).get(width), base.get("browser", {}).get(width)
    browser = [
        cell(_read(read, seen), _read(read, seen_before), places, compared=compared) for _, read, places in BROWSER
    ]
    return [_page(entry) if first else "", width, *server, *browser]


def _page(entry: dict[str, Any]) -> str:
    """The page, and the status it answered when that was not 200."""

    status = entry.get("server", {}).get("status", 200)
    return entry["page"] if status == 200 else f"{entry['page']} ({status})"


def _total(entries: list[dict[str, Any]], read: Read, key: str, width: str | None) -> float | None:
    values = [_read(read, entry.get(key) if width is None else entry.get(key, {}).get(width)) for entry in entries]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _totals(report: Report, base: Report | None, width: str, *, first: bool) -> list[str]:
    compared = base is not None
    mine, theirs = report["pages"], (base or {"pages": []})["pages"]
    server = [
        cell(_total(mine, read, "server", None), _total(theirs, read, "server", None), places, compared=compared)
        if first
        else ""
        for _, read, places in SERVER
    ]
    server += [""] * (len(server_headers(compared=compared)) - len(SERVER))
    browser = [
        cell(_total(mine, read, "browser", width), _total(theirs, read, "browser", width), places, compared=compared)
        for _, read, places in BROWSER
    ]
    return ["total" if first else "", width, *server, *browser]


def aligned(rows: list[list[str]]) -> list[str]:
    sizes = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        "  ".join(
            cell.ljust(size) if column == 0 else cell.rjust(size)
            for column, (cell, size) in enumerate(zip(row, sizes, strict=True))
        ).rstrip()
        for row in rows
    ]


def _summary(report: Report, base: Report | None) -> list[str]:
    """Memory and cold start, under the table."""

    compared = base is not None
    lines = []
    for label, read, places, unit in SUMMARY:
        said = cell(_read(read, report), _read(read, base), places, compared=compared)
        lines.append(f"{label}: {said} {unit}")
    return lines


def _title(report: Report, base: Report | None) -> str:
    title = f"{report['commit']} ({report['target']}, {report['plan']['seed']}, {report['requests']} requests a page)"
    if base:
        title += f", beside {base['commit']}, the two servers asked in turn"
    if memory := (report.get("memory") or {}).get("measured"):
        title += f"; memory is {memory}"
    return title


def _table(report: Report, base: Report | None, *, every_page: bool) -> list[str]:
    compared = base is not None
    theirs = _pages(base) if base else {}
    rows = [headers(compared=compared)]
    for entry in report["pages"] if every_page else []:
        for index, width in enumerate(_widths(report)):
            rows.append(_row(entry, theirs.get(entry["page"]), width, first=index == 0, compared=compared))
    for index, width in enumerate(_widths(report)):
        rows.append(_totals(report, base, width, first=index == 0))
    return aligned(rows)


def _p50(entry: dict[str, Any]) -> float | None:
    return _read(SERVER[0][1], entry.get("server"))


def moved(report: Report, base: Report | None = None) -> list[str]:
    """Beside a base, the pages whose server time moved past its spread, the furthest first; without
    one the SLOWEST pages; each with every server number."""

    if base is None:
        timed = [entry for entry in report["pages"] if _p50(entry) is not None]
        picked = sorted(timed, key=lambda entry: -_p50(entry))[:SLOWEST]
        heading = "The slowest pages by server p50:"
    else:
        past = [entry for entry in report["pages"] if past_spread(entry.get("server") or {})]
        picked = sorted(past, key=lambda entry: -abs(entry["server"]["moved_ms"]))
        heading = "The pages whose server time moved past its spread:"
        if not picked:
            return ["No page's server time moved past its spread."]
    if not picked:
        return []
    theirs = _pages(base) if base else {}
    rows = [["page", *server_headers(compared=base is not None)]]
    rows += [[_page(entry), *_server(entry, theirs.get(entry["page"]), compared=base is not None)] for entry in picked]
    return [heading, *aligned(rows)]


def render(report: Report, base: Report | None = None) -> str:
    """The table, then memory and cold start; with ``base``, each number beside how far it moved."""

    return "\n".join([_title(report, base), *_table(report, base, every_page=True), "", *_summary(report, base)])


def summary(report: Report, base: Report | None, table: str) -> str:
    """The totals, memory, cold start and the pages that moved most, naming ``table``, where every page is."""

    return "\n".join(
        [
            _title(report, base),
            *_table(report, base, every_page=False),
            "",
            *_summary(report, base),
            "",
            *moved(report, base),
            "",
            f"Every page at each width: {table}",
        ]
    )
