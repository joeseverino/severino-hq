"""What the measuring tools print, read into numbers."""

import re
import statistics
from html.parser import HTMLParser
from typing import override

from tools.pipeline.release_readings import keyed

# One metric of a Server-Timing header: its name, then `;dur=<ms>` among its parameters.
_METRIC = re.compile(r"(?:^|,)\s*([!#$%&'*+.^_`|~\w-]+)((?:\s*;[^,]*)*)")
_DURATION = re.compile(r";\s*dur\s*=\s*([0-9.]+)")
# `footprint -f bytes` prints each process's resident footprint as this line.
_FOOTPRINT = re.compile(r"^\s*phys_footprint:\s*(\d+)\s*B\s*$", re.MULTILINE)


def server_time(header: str, metric: str = "app") -> float | None:
    """The duration, in milliseconds, a Server-Timing header gives ``metric``; None without one."""

    for match in _METRIC.finditer(header or ""):
        if match.group(1) == metric and (duration := _DURATION.search(match.group(2))):
            return float(duration.group(1))
    return None


def percentiles(values: list[float]) -> tuple[float, float]:
    """The median and the 95th percentile of at least one value, each interpolated between the
    two values either side of it."""

    cuts = statistics.quantiles(values, n=20, method="inclusive")
    return round(cuts[9], 2), round(cuts[18], 2)


def footprint_bytes(output: str) -> int:
    """The footprint of every process `footprint -f bytes` reported, together."""

    found = [int(value) for value in _FOOTPRINT.findall(output)]
    if not found:
        raise ValueError(f"footprint reported no process: {output.strip()[:200]!r}")
    return sum(found)


def container_anon_bytes(memory_stat: str) -> int:
    """The anonymous memory a container's processes hold, from its cgroup's memory.stat."""

    anon = keyed(memory_stat).get("anon")
    if anon is None:
        raise ValueError(f"memory.stat names no anonymous memory: {memory_stat.strip()[:80]!r}")
    return anon


# Elements that never hold others, so never close.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
# A table body's rows that are no record: a group's heading, and the line held beneath an open row.
_NOT_RECORDS = frozenset({"row-group", "row-detail"})


class _Rows(HTMLParser):
    """Counts the records a reader meets in a page's tables, as the browser gates select them
    (`main .data-table > tbody > tr`), leaving out the rows that hold no record of their own."""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[tuple[str, frozenset[str]]] = []
        self.rows = 0

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = frozenset((dict(attrs).get("class") or "").split())
        if tag == "tr" and self._in_body() and not classes & _NOT_RECORDS:
            self.rows += 1
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
                return


def data_rows(html: str) -> int:
    """How many records the tables of a page's main region hold."""

    reader = _Rows()
    reader.feed(html)
    reader.close()
    return reader.rows


def cookie(output: str) -> tuple[str, str]:
    """(name, value) from the `<name>=<value>` line `manage.py dev_session` ends on."""

    lines = output.strip().splitlines()
    name, _, value = lines[-1].partition("=") if lines else ("", "", "")
    if not name or not value or any(char.isspace() for char in name + value):
        raise ValueError(f"dev_session printed no cookie: {output.strip()[-200:]!r}")
    return name, value
