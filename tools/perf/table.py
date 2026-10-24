"""One report, or one beside its base, as the table `perf:report` prints.

A row is a page at one width: the server's numbers on its first width's row,
the browser's on each. A total row sums each column over every page, so a
change's whole effect reads as one number per column. With a base, each number
is followed by how far it moved from the base's.
"""

from collections.abc import Callable
from typing import Any

Report = dict[str, Any]
Read = Callable[[dict[str, Any]], float | None]

# The server's columns, read from a page's `server`: header, value, decimals.
SERVER: tuple[tuple[str, Read, int], ...] = (
    ("p50 ms", lambda server: server.get("p50_ms"), 1),
    ("p95 ms", lambda server: server.get("p95_ms"), 1),
    ("queries", lambda server: server.get("queries"), 0),
)
# The browser's, read from a page's `browser[width]`.
BROWSER: tuple[tuple[str, Read, int], ...] = (
    ("first KB", lambda seen: _kilobytes(seen["first"]["bytes"]), 1),
    ("first req", lambda seen: seen["first"]["requests"], 0),
    ("repeat KB", lambda seen: _kilobytes(seen["repeat"]["bytes"]), 1),
    ("repeat req", lambda seen: seen["repeat"]["requests"], 0),
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
HEADERS = ("page", "width", *(name for name, _, _ in SERVER), *(name for name, _, _ in BROWSER))


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


def _cell(value: float | None, base: float | None, places: int, compared: bool) -> str:
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


def _row(entry: dict[str, Any], base: dict[str, Any] | None, width: str, first: bool, compared: bool) -> list[str]:
    base = base or {}
    server = [
        _cell(_read(read, entry.get("server")), _read(read, base.get("server")), places, compared) if first else ""
        for _, read, places in SERVER
    ]
    seen, seen_before = entry.get("browser", {}).get(width), base.get("browser", {}).get(width)
    browser = [_cell(_read(read, seen), _read(read, seen_before), places, compared) for _, read, places in BROWSER]
    return [_page(entry) if first else "", width, *server, *browser]


def _page(entry: dict[str, Any]) -> str:
    """The page, and the status it answered when that was not 200."""

    status = entry.get("server", {}).get("status", 200)
    return entry["page"] if status == 200 else f"{entry['page']} ({status})"


def _total(entries: list[dict[str, Any]], read: Read, key: str, width: str | None) -> float | None:
    values = [_read(read, entry.get(key) if width is None else entry.get(key, {}).get(width)) for entry in entries]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _totals(report: Report, base: Report | None, width: str, first: bool) -> list[str]:
    compared = base is not None
    mine, theirs = report["pages"], (base or {"pages": []})["pages"]
    server = [
        _cell(_total(mine, read, "server", None), _total(theirs, read, "server", None), places, compared)
        if first
        else ""
        for _, read, places in SERVER
    ]
    browser = [
        _cell(_total(mine, read, "browser", width), _total(theirs, read, "browser", width), places, compared)
        for _, read, places in BROWSER
    ]
    return ["total" if first else "", width, *server, *browser]


def _aligned(rows: list[list[str]]) -> list[str]:
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
        said = _cell(_read(read, report), _read(read, base), places, compared)
        lines.append(f"{label}: {said} {unit}")
    return lines


def render(report: Report, base: Report | None = None) -> str:
    """The table, then memory and cold start; with ``base``, each number beside how far it moved."""

    compared = base is not None
    theirs = _pages(base) if base else {}
    rows = [list(HEADERS)]
    for entry in report["pages"]:
        for index, width in enumerate(_widths(report)):
            rows.append(_row(entry, theirs.get(entry["page"]), width, index == 0, compared))
    for index, width in enumerate(_widths(report)):
        rows.append(_totals(report, base, width, index == 0))
    title = f"{report['commit']} ({report['target']}, {report['requests']} requests a page)"
    if base:
        title += f", beside {base['commit']}"
    if memory := report.get("memory", {}).get("measured"):
        title += f"; memory is {memory}"
    return "\n".join([title, *_aligned(rows), "", *_summary(report, base)])
