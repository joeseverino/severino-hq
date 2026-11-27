"""What HQ can see: every kind the controller reads, by the connection
provider that takes it, and how current each is."""

from dataclasses import dataclass
from typing import Any

lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.providers import provider_reads

from ..labels import dotted
from ..projection import iso, read_once
from ..tables import Cell, DataTable, Group, TableColumn
from ..tone import Tone
from ..ui import counted
from .band import Tile
lazy from ..connections import connection_rows
lazy from ..credential_sight import (
    NEVER_SWEPT,
    NOT_CONNECTED,
    READABLE,
    Standing,
    sight as sight_of,
)
lazy from ..facts import stored_snapshots
lazy from ..freshness import OUT_OF_DATE
lazy from ..routes import reverse

# The group of readings no connection holds a credential for.
KEYLESS = "Needs no connection"


@dataclass(frozen=True, slots=True)
class HqSight:
    """Every reading kind, by the connection provider that takes it."""

    groups: tuple[tuple[str, tuple[Standing, ...]], ...] = ()
    # Providers no connection reads, by label.
    unconnected: tuple[str, ...] = ()

    @property
    def rows(self) -> tuple[Standing, ...]:
        return tuple(row for _label, rows in self.groups for row in rows)

    @property
    def read(self) -> int:
        return sum(row.read for row in self.rows)

    @property
    def stale(self) -> int:
        return sum(row.stale for row in self.rows)

    @property
    def never(self) -> int:
        """Listed kinds no sweep has read yet. One read when a person opens it
        is owed nothing until someone does."""

        return sum(
            row.sight.state == NEVER_SWEPT and not ((spec := OBSERVATIONS.get(row.sight.kind)) and spec.on_open)
            for row in self.rows
        )

    @property
    def failing(self) -> int:
        """Kinds a connection tried and could not read; not-connected ones are not failures."""

        return sum(row.failed for row in self.rows)

    @property
    def said(self) -> str:
        """How current the readings are, in the one wording HQ's page and the Controller's say it."""

        return (
            dotted(
                f"{self.stale} {OUT_OF_DATE.lower()}" if self.stale else "",
                f"{self.failing} could not be read" if self.failing else "",
                f"{self.never} never read" if self.never else "",
            )
            or "All current"
        )

    @property
    def tone(self) -> Tone:
        return Tone.ATTENTION if self.stale or self.failing or self.never else Tone.GOOD

    @property
    def table(self) -> DataTable:
        """Every reading kind under the provider that takes it."""

        return DataTable(
            "Readings by connection",
            _SEES_COLUMNS,
            groups=tuple(
                Group(provider, DataTable.of("", _SEES_COLUMNS, rows).rows, count=len(rows))
                for provider, rows in self.groups
            ),
        )


def _reading(row: Standing) -> Cell:
    """A reading kind, with why it is not read beneath: each refused part's
    reason, else its remedy, else the error that stopped it."""

    seen = row.sight
    if seen.part_reasons:
        said = seen.part_reasons
    elif seen.remedy:
        said = (seen.remedy,)
    else:
        said = (seen.error,) if seen.error and not row.read else ()
    return Cell(seen.label, lines=tuple(Cell(line, muted=True) for line in said))


def _state(row: Standing) -> Cell:
    """Its state, in bold unless it reads as it should."""

    return Cell(row.label, name=row.state != READABLE)


_SEES_COLUMNS = (
    TableColumn("Reading", cell=_reading),
    TableColumn("State", kind="status", css="key-col", cell=_state),
    TableColumn("Records", kind="numeric", drop=2, cell=lambda row: Cell(str(row.sight.records) if row.read else "")),
    TableColumn(
        "Reading", kind="status", css="optional-col", drop=1, cell=lambda row: Cell.reading(at=row.sight.observed_at)
    ),
)


def sight() -> HqSight:
    """Every kind the controller reads, by provider, as last read: what the
    Controller page lists and HQ's page counts."""

    return read_once("hq_context.sight", _sight)


def _sight() -> HqSight:
    stored = {kind: rows[0] for kind, rows in stored_snapshots().items() if rows}
    reported = {CONNECTION_LABELS.get(row.provider) for row in connection_rows()}
    groups: dict[str, list[Standing]] = {}
    # Each inventory kind once, under the provider that reads it whole.
    for provider, reads in provider_reads().items():
        for read in reads:
            if read.source == "part":
                continue
            seen = sight_of(read.kind, read.label, read.source, stored.get(read.kind), permissions=read.permissions)
            groups.setdefault(CONNECTION_LABELS.get(provider, KEYLESS), []).append(Standing.of(seen))
    # A provider that takes a connection and has none is said in one line; a
    # reading that needs no connection is always listed, read or not, and so is
    # every reading of a connection the controller has reported, read yet or not.
    shown = {
        label: tuple(rows)
        for label, rows in groups.items()
        if label == KEYLESS
        or label in reported
        or any(row.sight.state not in (NOT_CONNECTED, NEVER_SWEPT) for row in rows)
    }
    return HqSight(
        groups=tuple(sorted(shown.items(), key=lambda group: (group[0] == KEYLESS, group[0]))),
        unconnected=tuple(sorted(label for label in groups if label not in shown)),
    )


def sight_tile(seen: HqSight) -> Tile:
    return Tile(
        "Sees", counted(seen.read, "reading"), seen.said, reverse("control_plane:connections"), seen.tone, "eye"
    )


def sight_json(seen: HqSight) -> dict[str, Any]:
    return {
        "read": seen.read,
        "out_of_date": seen.stale,
        "could_not_read": seen.failing,
        "never_read": seen.never,
        "not_connected": list(seen.unconnected),
        "readings": [
            {
                "kind": row.sight.kind,
                "label": row.sight.label,
                "provider": provider,
                "state": row.sight.state,
                "records": row.sight.records,
                "observed_at": iso(row.sight.observed_at),
                "out_of_date": row.stale,
                "remedy": row.sight.remedy,
                "error": row.sight.error,
            }
            for provider, rows in seen.groups
            for row in rows
        ],
    }
