"""Where a connection is in its life: the one vocabulary every view says it in.

A reading, a catalog view, a connections-page row and the estate's own count
each arrive at a state from different evidence, and say it in these words.
Two facts outrank whatever the evidence says: a connection its controller no
longer reports has no current reading at all, and a credential the service
refused is refused whatever a probe found. ``standing`` holds that order.
"""

from dataclasses import dataclass
from enum import StrEnum


class Lifecycle(StrEnum):
    CONFIGURED = "configured"
    UNPROBED = "unprobed"
    REACHABLE = "reachable"
    READY = "ready"
    UNREACHABLE = "unreachable"
    UNAUTHORIZED = "unauthorized"
    STALE = "stale"
    REVOKED = "revoked"
    # Its controller's report came with no connections, and the row is what
    # HQ kept of it.
    UNREPORTED = "unreported"

    @property
    def label(self) -> str:
        """The one word the State column shows."""

        return _SAID[self].label

    @property
    def needs_attention(self) -> bool:
        return _SAID[self].attention


@dataclass(frozen=True, slots=True)
class _Said:
    label: str
    # Whether a person has to act: stale evidence, missing access, a failed
    # probe, a refused credential, a connection gone from its report.
    attention: bool = False


_SAID = {
    Lifecycle.CONFIGURED: _Said("Set up"),
    Lifecycle.UNPROBED: _Said("Not tested"),
    Lifecycle.REACHABLE: _Said("Working"),
    Lifecycle.READY: _Said("Working"),
    Lifecycle.UNREACHABLE: _Said("Not answering", attention=True),
    Lifecycle.UNAUTHORIZED: _Said("Permission missing", attention=True),
    Lifecycle.STALE: _Said("Out of date", attention=True),
    Lifecycle.REVOKED: _Said("Credential refused", attention=True),
    Lifecycle.UNREPORTED: _Said("No longer reported", attention=True),
}

# The states in which the connection works.
WORKING = frozenset({Lifecycle.READY, Lifecycle.REACHABLE})
ATTENTION = frozenset(state for state in Lifecycle if state.needs_attention)

# The unreported state as other records carry it, with the values they already
# stored: the fact a topology node holds when it was reported (its value the
# moment), and the id prefix of the node that stands in for a connection no
# controller reports.
UNREPORTED_FACT = f"connection-{Lifecycle.UNREPORTED}"
UNREPORTED_NODE = f"connection:{Lifecycle.UNREPORTED}:"


def probed(*, reachable: bool, was_probed: bool) -> Lifecycle:
    """What a controller's probe of a connection found."""

    if not reachable:
        return Lifecycle.UNREACHABLE
    return Lifecycle.REACHABLE if was_probed else Lifecycle.UNPROBED


def standing(found: Lifecycle, *, unreported: bool = False, refused: bool = False) -> Lifecycle:
    """``found``, unless the connection is unreported or its credential refused."""

    if unreported:
        return Lifecycle.UNREPORTED
    if refused:
        return Lifecycle.REVOKED
    return found
