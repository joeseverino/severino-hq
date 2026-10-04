"""The parts of a reading refused while the rest read, as a sweep collects them.

A reader reports one declared part it could not read with ``refuse_part``; the
sweep opens a ``part_ledger`` around each kind and reports what it collected
beside the kind's records (``control_plane.reading_parts``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .contracts import failure_of

_PART_REFUSALS: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "part_refusals", default=None
)


# How much of a failure's text a report carries: remote and refusal text can be
# long, and a report is read on a page.
REPORT_TEXT_LIMIT = 500


def report_text(text: str) -> str:
    """Failure text as a report carries it, cut to REPORT_TEXT_LIMIT."""

    return text[:REPORT_TEXT_LIMIT]


def unread_reason(exc: BaseException) -> str:
    """Why an optional read failed, short enough to show on a page."""

    return (str(exc).strip() or type(exc).__name__)[:200]


def refuse_part(
    part: str,
    exc: BaseException,
    *,
    scope: str = "",
    connection_ref: str = "",
    address: str = "",
) -> None:
    """Report one declared part of the reading in progress as refused: which
    part, why, and on what (a zone, a record's name, a machine, or "" for all
    of it). ``address`` is the machine's address when the scope is a machine.
    Never a record: the sweep reports it beside the kind's records."""

    ledger = _PART_REFUSALS.get()
    if ledger is not None:
        ledger.append(
            {
                "part": part,
                "refusal": failure_of(exc),
                "reason": unread_reason(exc),
                "scope": scope,
                "connection_ref": connection_ref,
                **({"address": address} if address else {}),
            }
        )


@contextmanager
def part_ledger() -> Iterator[list[dict[str, str]]]:
    """Collect the parts refused while one kind is read."""

    refused: list[dict[str, str]] = []
    token = _PART_REFUSALS.set(refused)
    try:
        yield refused
    finally:
        _PART_REFUSALS.reset(token)
