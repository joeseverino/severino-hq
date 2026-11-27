"""What HQ can see: every kind the controller reads, by the connection
provider that takes it, and how current each is."""

from dataclasses import dataclass
from typing import Any

from django.utils.text import capfirst

lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.providers import Read, provider_reads

from ..labels import dotted
from ..projection import iso, read_once
from ..tables import Cell, DataTable, Group, Row, TableColumn
from ..tone import Tone
from ..ui import counted
from .band import Tile
lazy from ..cadence import every_sweep, swept
lazy from ..connections import connection_rows
lazy from ..credential_sight import (
    NEVER_SWEPT,
    NOT_CONNECTED,
    READABLE,
    Standing,
    sight as sight_of,
)
lazy from ..facts import stored_snapshots
lazy from ..freshness import LAST_READ, OUT_OF_DATE
lazy from ..inbound_events import covers
lazy from ..routes import reverse

# The group of readings no connection holds a credential for.
KEYLESS = "Needs no connection"
# The group of readings a controller gathers through several connections, so
# no one provider takes them (a machine's figures).
SEVERAL = "Through several connections"


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
    def sees(self) -> str:
        """How many readings the table lists, as HQ's page and the Controller's count them."""

        return counted(len(self.rows), "reading")

    @property
    def stale(self) -> int:
        return sum(row.stale for row in self.rows)

    @property
    def never(self) -> int:
        """Listed kinds no sweep has read yet, each a "Never read" row of the table."""

        return sum(row.sight.state == NEVER_SWEPT for row in self.rows)

    @property
    def owed(self) -> int:
        """Of those never read, the ones owed a reading: one read when a person
        opens it is owed nothing until someone does."""

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
        return Tone.ATTENTION if self.stale or self.failing or self.owed else Tone.GOOD

    @property
    def table(self) -> DataTable:
        """Every reading kind under the provider that takes it."""

        return DataTable(
            "Readings by connection",
            _SEES_COLUMNS,
            groups=tuple(
                Group(provider, tuple(row for standing in rows for row in _rows(standing)), count=len(rows))
                for provider, rows in self.groups
            ),
        )


def _rows(standing: Standing) -> tuple[Row, ...]:
    """A reading's row, and folded under it each reason its refused parts
    were not read for."""

    cells = tuple(column.cell(standing) for column in _SEES_COLUMNS if column.cell)
    reasons = standing.sight.part_reasons
    if not reasons:
        return (Row(cells),)
    fold = f"reading-{standing.sight.kind.replace('.', '-')}"
    return (
        Row(cells, folds=fold),
        *(Row((Cell(reason, muted=True),), folded=fold, across=True) for reason in reasons),
    )


def _reading(row: Standing) -> Cell:
    """A reading kind, with why it is not read beneath: how many of its parts
    were refused, whose reasons its row folds, else its remedy, else the
    error that stopped it."""

    seen = row.sight
    if seen.part_reasons:
        unread = {part.part.label for part in seen.parts}
        said: tuple[str, ...] = (f"{counted(len(unread), 'part')} not read",)
    elif seen.remedy:
        said = (seen.remedy,)
    else:
        said = (seen.error,) if seen.error and not row.read else ()
    return Cell(
        seen.label, title=seen.kind, fold=bool(seen.part_reasons), lines=tuple(Cell(line, muted=True) for line in said)
    )


def _age(row: Standing) -> Cell:
    """When it was last read, the moment alone since its column's header and
    its state say the rest, with its own clock beneath where it is read
    slower than every sweep, and when it was tried where that try failed."""

    seen = row.sight
    clock = (
        (Cell(capfirst(f"{row.age.cadence}{' and on each event' * covers(seen.kind)}"), muted=True),)
        if swept(seen.kind) and not every_sweep(seen.kind)
        else ()
    )
    tried = (
        (Cell("Tried", muted=True, parts=(Cell(moment=seen.tried_at, muted=True),)),)
        if row.failed and seen.tried_at
        else ()
    )
    return Cell(moment=seen.observed_at, lines=clock + tried)


def _state(row: Standing) -> Cell:
    """Its state, in bold unless it reads as it should."""

    return Cell(row.label, name=row.state != READABLE)


_SEES_COLUMNS = (
    TableColumn("Reading", cell=_reading),
    TableColumn("State", kind="status", css="key-col", cell=_state),
    TableColumn("Records", kind="numeric", drop=2, cell=lambda row: Cell(str(row.sight.records) if row.read else "")),
    TableColumn(LAST_READ, kind="status", css="optional-col", drop=1, cell=_age),
)


def sight() -> HqSight:
    """Every kind the controller reads, by provider, as last read: what the
    Controller page lists and HQ's page counts."""

    return read_once("hq_context.sight", _sight)


def _sight() -> HqSight:
    stored = {kind: rows[0] for kind, rows in stored_snapshots().items() if rows}
    reported = {CONNECTION_LABELS.get(row.provider) for row in connection_rows()}
    groups: dict[str, list[Standing]] = {}
    # Each inventory kind once, under the provider that reads it whole, and
    # each reading no one provider takes under SEVERAL.
    several = tuple(
        Read(kind, spec.label, "reading", spec.read_by, tuple(spec.requires))
        for kind, spec in OBSERVATIONS.items()
        if spec.provider is None
    )
    for provider, reads in (*provider_reads().items(), (None, several)):
        for read in reads:
            if read.source == "part":
                continue
            seen = sight_of(read.kind, read.label, read.source, stored.get(read.kind), permissions=read.permissions)
            label = SEVERAL if provider is None else CONNECTION_LABELS.get(provider, KEYLESS)
            groups.setdefault(label, []).append(Standing.of(seen))
    # A provider that takes a connection and has none is said in one line; a
    # reading that needs no connection, or no one connection, is always listed,
    # read or not, and so is
    # every reading of a connection the controller has reported, read yet or not.
    shown = {
        label: tuple(rows)
        for label, rows in groups.items()
        if label in (KEYLESS, SEVERAL)
        or label in reported
        or any(row.sight.state not in (NOT_CONNECTED, NEVER_SWEPT) for row in rows)
    }
    return HqSight(
        groups=tuple(sorted(shown.items(), key=lambda group: (group[0] == KEYLESS, group[0]))),
        unconnected=tuple(sorted(label for label in groups if label not in shown)),
    )


def sight_tile(seen: HqSight) -> Tile:
    return Tile("Sees", seen.sees, seen.said, reverse("control_plane:connections"), seen.tone, "eye")


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
