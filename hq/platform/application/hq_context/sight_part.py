"""What HQ can see: every kind the controller reads, by the connection
provider that takes it, and how current each is."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.providers import PROVIDERS

from ..labels import dotted, human_label
from ..projection import iso, read_once
from ..tables import Cell, DataTable, Group, TableColumn
from ..tone import Tone
from ..ui import counted
from .band import Tile
lazy from ..connections import connection_rows
lazy from ..credential_sight import (
    NEVER_SWEPT,
    NOT_CONNECTED,
    PARTIAL,
    READABLE,
    REFUSED,
    UNREADABLE,
    sight as sight_of,
)
lazy from ..facts import stored_snapshots
lazy from ..freshness import freshness
lazy from ..routes import reverse

# The group of readings no connection holds a credential for.
KEYLESS = "Needs no connection"


@dataclass(frozen=True, slots=True)
class Sees:
    """One reading kind as HQ holds it."""

    kind: str
    label: str
    provider: str
    state: str
    state_label: str
    records: int
    observed_at: datetime | None
    stale: bool = False
    remedy: str = ""
    # Why refused parts were not read, one line per reason.
    reasons: tuple[str, ...] = ()
    error: str = ""

    @property
    def read(self) -> bool:
        return self.state in (READABLE, PARTIAL)


@dataclass(frozen=True, slots=True)
class HqSight:
    """Every reading kind, by the connection provider that takes it."""

    groups: tuple[tuple[str, tuple[Sees, ...]], ...] = ()
    # Providers no connection reads, by label.
    unconnected: tuple[str, ...] = ()

    @property
    def rows(self) -> tuple[Sees, ...]:
        return tuple(row for _label, rows in self.groups for row in rows)

    @property
    def read(self) -> int:
        return sum(row.read for row in self.rows)

    @property
    def stale(self) -> int:
        return sum(row.stale for row in self.rows)

    @property
    def never(self) -> int:
        """Listed kinds no sweep has read yet."""

        return sum(row.state == NEVER_SWEPT for row in self.rows)

    @property
    def failing(self) -> int:
        """Kinds a connection tried and could not read; not-connected ones are not failures."""

        return sum(row.state in (REFUSED, UNREADABLE) for row in self.rows)

    @property
    def said(self) -> str:
        """How current the readings are, in the one wording HQ's page and the Controller's say it."""

        return (
            dotted(
                f"{self.stale} out of date" if self.stale else "",
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


def _reading(row: Sees) -> Cell:
    """A reading kind, with why it is not read beneath: each refused part's
    reason, else its remedy, else the error that stopped it."""

    if row.reasons:
        said = row.reasons
    elif row.remedy:
        said = (row.remedy,)
    else:
        said = (row.error,) if row.error and not row.read else ()
    return Cell(row.label, lines=tuple(Cell(line, muted=True) for line in said))


def _state(row: Sees) -> Cell:
    """Its state, in bold unless it reads as it should."""

    if row.stale:
        return Cell("Out of date", name=True)
    return Cell(row.state_label, name=not (row.read and row.state == READABLE))


_SEES_COLUMNS = (
    TableColumn("Reading", cell=_reading),
    TableColumn("State", kind="status", css="key-col", cell=_state),
    TableColumn("Records", kind="numeric", drop=2, cell=lambda row: Cell(str(row.records) if row.read else "")),
    TableColumn(
        "Reading", kind="status", css="optional-col", drop=1, cell=lambda row: Cell.reading(at=row.observed_at)
    ),
)


def sight() -> HqSight:
    """Every kind the controller reads, by provider, as last read: what the
    Controller page lists and HQ's page counts."""

    return read_once("hq_context.sight", _sight)


def _read_kinds() -> Iterator[tuple[str, str, str, tuple[str, ...], str]]:
    """Each kind the sweep reads, as (kind, label, provider, permissions,
    source): every reading, then every resource kind whose records it reads."""

    for kind, spec in OBSERVATIONS.items():
        yield kind, spec.label, spec.provider, tuple(spec.requires), "reading"
    for kind, spec in PROVIDERS.items():
        if spec.connection_providers and not spec.unobserved_reason:
            yield kind, spec.label or human_label(kind), spec.connection_providers[0], (), "resource"


def _sight() -> HqSight:
    stored = {kind: rows[0] for kind, rows in stored_snapshots().items() if rows}
    reported = {CONNECTION_LABELS.get(row.provider) for row in connection_rows()}
    groups: dict[str, list[Sees]] = {}
    for kind, label, provider, permissions, source in _read_kinds():
        row = _sees(kind, label, provider, sight_of(kind, label, source, stored.get(kind), permissions=permissions))
        groups.setdefault(CONNECTION_LABELS.get(provider, KEYLESS), []).append(row)
    # A provider that takes a connection and has none is said in one line; a
    # reading that needs no connection is always listed, read or not, and so is
    # every reading of a connection the controller has reported, read yet or not.
    shown = {
        label: tuple(rows)
        for label, rows in groups.items()
        if label == KEYLESS or label in reported or any(row.state not in (NOT_CONNECTED, NEVER_SWEPT) for row in rows)
    }
    return HqSight(
        groups=tuple(sorted(shown.items(), key=lambda group: (group[0] == KEYLESS, group[0]))),
        unconnected=tuple(sorted(label for label in groups if label not in shown)),
    )


def _sees(kind: str, label: str, provider: str, seen: Any) -> Sees:
    return Sees(
        kind=kind,
        label=label,
        provider=provider,
        state=seen.state,
        state_label=seen.state_label,
        records=seen.records,
        observed_at=seen.observed_at,
        # Only a reading that was taken can be out of date; a refused one says so.
        stale=seen.state in (READABLE, PARTIAL) and freshness(kind, seen.observed_at).stale,
        remedy=seen.remedy,
        reasons=seen.part_reasons,
        error=seen.error,
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
                "kind": row.kind,
                "label": row.label,
                "provider": provider,
                "state": row.state,
                "records": row.records,
                "observed_at": iso(row.observed_at),
                "out_of_date": row.stale,
                "remedy": row.remedy,
                "error": row.error,
            }
            for provider, rows in seen.groups
            for row in rows
        ],
    }
