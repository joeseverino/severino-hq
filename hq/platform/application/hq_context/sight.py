"""What HQ can see: every registered reading kind, by the connection provider
that takes it, and how current each is."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..projection import read_once
from ..ui import counted
from .tones import ATTENTION, GOOD, Tile, iso

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
    error: str = ""

    @property
    def read(self) -> bool:
        from ..credential_sight import PARTIAL, READABLE

        return self.state in (READABLE, PARTIAL)


@dataclass(frozen=True, slots=True)
class Sight:
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

        from ..credential_sight import NEVER_SWEPT

        return sum(row.state == NEVER_SWEPT for row in self.rows)

    @property
    def failing(self) -> int:
        """Kinds a connection tried and could not read; not-connected ones are not failures."""

        from ..credential_sight import REFUSED, UNREADABLE

        return sum(row.state in (REFUSED, UNREADABLE) for row in self.rows)


def sight() -> Sight:
    """Every registered reading kind, by provider, as last read."""

    return read_once("hq_context.sight", _sight)


def _sight() -> Sight:
    from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
    from hq.domains.control_plane.observations import OBSERVATIONS

    from ..credential_sight import NEVER_SWEPT, NOT_CONNECTED
    from ..facts import stored_snapshots

    stored = {kind: rows[0] for kind, rows in stored_snapshots().items() if rows}
    groups: dict[str, list[Sees]] = {}
    for kind, spec in OBSERVATIONS.items():
        row = _sees(kind, spec, stored.get(kind))
        groups.setdefault(CONNECTION_LABELS.get(spec.provider, KEYLESS), []).append(row)
    # A provider that takes a connection and has none is said in one line; a
    # reading that needs no connection is always listed, read or not.
    shown = {
        label: tuple(rows)
        for label, rows in groups.items()
        if label == KEYLESS or any(row.state not in (NOT_CONNECTED, NEVER_SWEPT) for row in rows)
    }
    return Sight(
        groups=tuple(sorted(shown.items(), key=lambda group: (group[0] == KEYLESS, group[0]))),
        unconnected=tuple(sorted(label for label in groups if label not in shown)),
    )


def _sees(kind: str, spec: Any, row: Any) -> Sees:
    from ..credential_sight import PARTIAL, READABLE, sight as sight_of
    from ..freshness import freshness

    seen = sight_of(kind, spec.label, "reading", row, permissions=tuple(spec.requires))
    return Sees(
        kind=kind,
        label=spec.label,
        provider=spec.provider,
        state=seen.state,
        state_label=seen.state_label,
        records=seen.records,
        observed_at=seen.observed_at,
        # Only a reading that was taken can be out of date; a refused one says so.
        stale=seen.state in (READABLE, PARTIAL) and freshness(kind, seen.observed_at).stale,
        remedy=seen.remedy,
        error=seen.error,
    )


def sight_tile(seen: Sight) -> Tile:
    from ..routes import reverse

    detail = " · ".join(
        part
        for part in (
            f"{seen.stale} out of date" if seen.stale else "",
            f"{seen.failing} could not be read" if seen.failing else "",
            f"{seen.never} never read" if seen.never else "",
        )
        if part
    )
    return Tile(
        "Sees",
        counted(seen.read, "reading"),
        detail or "All current",
        reverse("control_plane:connections"),
        ATTENTION if seen.stale or seen.failing or seen.never else GOOD,
        "eye",
    )


def sight_json(seen: Sight) -> dict[str, Any]:
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
