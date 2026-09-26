"""What each connection provider's credential can see, as the last sweep found it.

Derived from the two registries: ``OBSERVATIONS`` names the readings a
provider feeds, and ``PROVIDERS`` names the resource kinds that list it in
``connection_providers``. A resource kind that declares ``unobserved_reason``
is read by no sweep and is left out. Each kind is joined to its
``ProviderInventory`` row, read once for the whole page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from control_plane.models import ProviderInventory
from control_plane.observations import OBSERVATIONS
from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
)
from control_plane.providers import CONNECTION_CREDENTIALS, CONNECTION_LABELS, PROVIDERS

from .labels import human_label

READABLE = "readable"
REFUSED = "refused"
UNREADABLE = "unreadable"
NOT_CONNECTED = "not_connected"
NEVER_SWEPT = "never_swept"

STATE_LABELS = {
    READABLE: "Readable",
    REFUSED: "Refused",
    UNREADABLE: "Unreadable",
    NOT_CONNECTED: "Not connected",
    NEVER_SWEPT: "Never swept",
}


@dataclass(frozen=True)
class Sight:
    """One reading or resource kind, and whether the credential can read it."""

    kind: str
    label: str
    # "reading" or "resource".
    source: str
    state: str
    records: int = 0
    observed_at: datetime | None = None
    error: str = ""
    requires: str = ""
    # One of REFUSALS when the provider refused the read.
    refusal: str = ""

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def remedy(self) -> str:
        if self.refusal != PERMISSION_REFUSAL or not self.requires:
            return ""
        return f"Add {self.requires} to see {self.label}"


@dataclass(frozen=True)
class ProviderSight:
    provider: str
    label: str
    sights: tuple[Sight, ...]
    # Labels of the resource kinds this provider's credential acts on.
    manages: tuple[str, ...] = ()
    # Why the provider refused the credential itself, or "" when it did not.
    credential_refusal: str = ""

    def _count(self, state: str) -> int:
        return sum(sight.state == state for sight in self.sights)

    @property
    def tally(self) -> tuple[tuple[int, str], ...]:
        """(count, state label) for each state present, in STATE_LABELS order."""

        return tuple(
            (count, label)
            for state, label in STATE_LABELS.items()
            if (count := self._count(state))
        )

    @property
    def sees(self) -> tuple[str, ...]:
        return tuple(sight.label for sight in self.sights)


def sight(
    kind: str,
    label: str,
    source: str,
    row: ProviderInventory | None,
    *,
    requires: str = "",
) -> Sight:
    if row is None:
        return Sight(kind, label, source, NEVER_SWEPT, requires=requires)
    if not row.connected:
        return Sight(
            kind, label, source, NOT_CONNECTED, observed_at=row.observed_at, requires=requires
        )
    if not row.reachable:
        return Sight(
            kind,
            label,
            source,
            REFUSED if row.refusal else UNREADABLE,
            observed_at=row.observed_at,
            # A refused credential is said once for the provider, not per kind.
            error="" if row.refusal == CREDENTIAL_REFUSAL else row.error,
            requires=requires,
            refusal=row.refusal,
        )
    return Sight(
        kind,
        label,
        source,
        READABLE,
        records=len(row.records or ()),
        observed_at=row.observed_at,
        requires=requires,
    )


def _refused_credential_reason(rows: list[ProviderInventory]) -> str:
    """The provider's words for refusing the credential, from any kind it refused."""

    return next(
        (row.error for row in rows if row.refusal == CREDENTIAL_REFUSAL and row.error),
        "",
    )


def credential_sight() -> tuple[ProviderSight, ...]:
    """Every credential-holding provider and what its credential can see."""

    inventory = {
        row.kind: row
        for row in ProviderInventory.objects.only(
            "kind", "records", "reachable", "connected", "error", "refusal", "observed_at"
        )
    }
    found: dict[str, list[Sight]] = {provider: [] for provider in CONNECTION_CREDENTIALS}
    rows: dict[str, list[ProviderInventory]] = {}
    manages: dict[str, list[str]] = {}
    for kind, spec in OBSERVATIONS.items():
        if spec.provider not in found:
            continue
        row = inventory.get(kind)
        found[spec.provider].append(
            sight(kind, spec.label, "reading", row, requires=", ".join(spec.requires))
        )
        if row is not None:
            rows.setdefault(spec.provider, []).append(row)
    for kind, spec in PROVIDERS.items():
        label = spec.label or human_label(kind)
        for provider in spec.connection_providers:
            manages.setdefault(provider, []).append(label)
            if spec.unobserved_reason or provider not in found:
                continue
            row = inventory.get(kind)
            found[provider].append(sight(kind, label, "resource", row))
            if row is not None:
                rows.setdefault(provider, []).append(row)
    return tuple(
        ProviderSight(
            provider,
            CONNECTION_LABELS[provider],
            tuple(sorted(sights, key=lambda sight: (sight.source, sight.label))),
            tuple(sorted(set(manages.get(provider, ())))),
            _refused_credential_reason(rows.get(provider, [])),
        )
        for provider, sights in sorted(found.items())
    )


def sight_by_connection(
    connected: set[str],
) -> tuple[dict[str, ProviderSight], tuple[ProviderSight, ...]]:
    """What each connected provider sees, and the providers with no connection.

    With nothing connected at all no controller has reported, so no provider is
    said to be missing one.
    """

    sights = credential_sight()
    return (
        {sight.provider: sight for sight in sights if sight.provider in connected},
        tuple(sight for sight in sights if sight.provider not in connected)
        if connected
        else (),
    )
