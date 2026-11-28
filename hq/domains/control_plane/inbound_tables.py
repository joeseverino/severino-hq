"""The events page's tables: one page of the events list, a group for each
channel that told HQ, and every event HQ takes, a group for each provider."""

from dataclasses import replace
from typing import Any

from hq.domains.control_plane.models import InboundOutcome, InboundSource
from hq.platform.application.entity_links import kind_label, provider_label
from hq.platform.application.inbound_events import Taken
from hq.platform.application.moments import moment
from hq.platform.application.quoted import Quoted
from hq.platform.application.tables import Cell, DataTable, Group, Row, TableColumn

from .provider_adapters import INBOUND


def _event(item: dict[str, Any]) -> Cell:
    """The event by the provider's name for it, and who sent it. Plain text,
    as a readout would draw "push" in words and "pull_request" in code."""

    return Cell(item["event"], lines=(Cell(item["provider_label"], muted=True),))


def _subject(item: dict[str, Any]) -> str:
    """The event's subject in the words its provider's declaration reads it in."""

    spec = INBOUND.get(item["provider"])
    return spec.worded(item["subject"]) if spec is not None else item["subject"]


EVENT_COLUMNS = (
    TableColumn("Event", kind="identifier", cell=_event),
    # Text HQ did not write: shown as the provider wrote it, and never a link.
    TableColumn("Subject", kind="identifier", drop=2, cell=lambda item: Cell(Quoted(_subject(item)), cut=True)),
    TableColumn(
        "Made due", drop=3, cell=lambda item: Cell(", ".join(kind_label(kind) for kind in item["reads"]), muted=True)
    ),
    TableColumn("Outcome", kind="status", css="key-col", cell=lambda item: Cell.state(InboundOutcome(item["outcome"]))),
    TableColumn("Received", sort="applied_at", cell=lambda item: Cell(moment=moment(item["applied_at"]))),
)


def grouped(table: DataTable, items: list[dict[str, Any]]) -> DataTable:
    """``table``'s rows, one per item in order, under the channel each came
    by: the channels in the order the list's sort first reaches each, and
    each channel's rows in that order too."""

    by_source: dict[str, list[Row]] = {}
    for item, row in zip(items, table.rows, strict=True):
        by_source.setdefault(item["source"], []).append(row)
    return replace(
        table,
        rows=(),
        groups=tuple(Group(InboundSource(source).label, tuple(rows)) for source, rows in by_source.items()),
    )


def _kinds(kinds: tuple[str, ...], *, muted: bool = False) -> Cell:
    return Cell(", ".join(kind_label(kind) for kind in kinds), muted=muted)


def _channel(taken: Taken) -> Cell:
    """The channel the events arrive by, and its standing now."""

    return Cell(taken.source.label, parts=(replace(Cell.state(taken.heard), dot=True),))


TAKEN_COLUMNS = (
    TableColumn("Events", cell=lambda taken: Cell(", ".join(taken.events))),
    TableColumn("Makes due", cell=lambda taken: _kinds(taken.reads)),
    TableColumn("Kept current", drop=2, cell=lambda taken: _kinds(taken.kept, muted=True)),
    TableColumn("Channel", kind="status", drop=1, cell=_channel),
)


def taken_table(taken: tuple[Taken, ...]) -> DataTable:
    """Every event HQ takes, under the provider that sends it, providers by name."""

    table = DataTable.of("What HQ takes", TAKEN_COLUMNS, taken, keeps_order=True)
    by_provider: dict[str, list[tuple[Taken, Row]]] = {}
    for each, row in zip(taken, table.rows, strict=True):
        by_provider.setdefault(provider_label(each.provider), []).append((each, row))
    return replace(
        table,
        rows=(),
        groups=tuple(
            Group(label, tuple(row for _each, row in rows), counted=True) for label, rows in sorted(by_provider.items())
        ),
    )
