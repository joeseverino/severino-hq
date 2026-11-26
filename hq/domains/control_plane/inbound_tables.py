"""The events page's table: one page of the events list, a group for each
channel that told HQ."""

from dataclasses import replace
from typing import Any

from hq.domains.control_plane.models import InboundOutcome, InboundSource
from hq.platform.application.entity_links import kind_label
from hq.platform.application.moments import moment
from hq.platform.application.tables import Cell, DataTable, Group, Row, TableColumn
from hq.platform.application.tone import Tone


def _event(item: dict[str, Any]) -> Cell:
    """The event by the provider's name for it, and who sent it."""

    return Cell(item["event"], lines=(Cell(item["provider_label"], muted=True),))


def _outcome(item: dict[str, Any]) -> Cell:
    outcome = InboundOutcome(item["outcome"])
    return Cell(outcome.label, tone=Tone.GOOD if outcome == InboundOutcome.APPLIED else "")


EVENT_COLUMNS = (
    TableColumn("Event", kind="identifier", cell=_event),
    # Text HQ did not write: shown as the provider wrote it, and never a link.
    TableColumn("Subject", kind="identifier", drop=2, cell=lambda item: Cell(item["subject"], readout=True, cut=True)),
    TableColumn(
        "Made due", drop=3, cell=lambda item: Cell(", ".join(kind_label(kind) for kind in item["reads"]), muted=True)
    ),
    TableColumn("Outcome", kind="status", css="key-col", cell=_outcome),
    TableColumn("Received", sort="received_at", cell=lambda item: Cell(moment=moment(item["received_at"]))),
)


def grouped(table: DataTable, items: list[dict[str, Any]]) -> DataTable:
    """``table``'s rows, one per item in order, under the channel each came
    by, each channel's rows in the list's own order."""

    by_source: dict[str, list[Row]] = {}
    for item, row in zip(items, table.rows, strict=True):
        by_source.setdefault(item["source"], []).append(row)
    return replace(
        table,
        rows=(),
        groups=tuple(
            Group(source.label, tuple(by_source[source]))
            for source in InboundSource
            if source in by_source
        ),
    )
