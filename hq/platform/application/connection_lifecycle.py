"""Where a connection is in its life: the one vocabulary every view says it in.

Every surface that says how a connection stands reads it from
``connections.connection_standing``, which composes the evidence through
``standing`` here, once per projection.
"""

from .references import Kind
from .tone import Tone, TonedChoices


class Lifecycle(TonedChoices):
    """Each state with its tone and the one word the State column shows:
    working is good, out of date is to be noticed, a failure is serious, and a
    connection never tested is neither. A person has to act on any state drawn
    above neutral (``Tone.acted_on``)."""

    CONFIGURED = "configured", Tone.NEUTRAL, "Added"
    UNPROBED = "unprobed", Tone.NEUTRAL, "Not tested"
    REACHABLE = "reachable", Tone.GOOD, "Working"
    UNREACHABLE = "unreachable", Tone.SERIOUS, "Not answering"
    UNAUTHORIZED = "unauthorized", Tone.SERIOUS, "Permission missing"
    STALE = "stale", Tone.ATTENTION, "Out of date"
    REVOKED = "revoked", Tone.SERIOUS, "Credential refused"
    # Its controller's report came with no connections, and the row is what
    # HQ kept of it.
    UNREPORTED = "unreported", Tone.SERIOUS, "No longer reported"


# The states in which the connection works: the ones drawn good.
WORKING = frozenset(state for state in Lifecycle if state.tone is Tone.GOOD)
ATTENTION = frozenset(state for state in Lifecycle if state.tone.acted_on)

# The unreported state as other records carry it, with the values they already
# stored: the fact a topology node holds when it was reported (its value the
# moment), and the id prefix of the node that stands in for a connection no
# controller reports.
UNREPORTED_FACT = f"connection-{Lifecycle.UNREPORTED}"
# The fact a connection's topology node carries its state in, for the findings
# that read the node.
STATE_FACT = "connection-state"
UNREPORTED_NODE = Kind.CONNECTION.of(f"{Lifecycle.UNREPORTED}:")


def probed(*, reachable: bool, was_probed: bool) -> Lifecycle:
    """What a controller's probe of a connection found."""

    if not reachable:
        return Lifecycle.UNREACHABLE
    return Lifecycle.REACHABLE if was_probed else Lifecycle.UNPROBED


def standing(
    found: Lifecycle | None,
    *,
    stale: bool = False,
    authority: str = "none",
    unreported: bool = False,
    refused: bool = False,
) -> Lifecycle:
    """One state from everything known of a connection, most telling first.

    ``found`` is what its last probe found (``probed``), None when nothing was
    ever observed. A connection its controller no longer reports has no current
    reading at all, and a credential the service refused is refused whatever a
    probe found. An old observation is stale before it is anything else,
    because every later claim rests on it, and missing access
    (``connection_catalog.connection_authority``) fails closed ahead of
    reachability.
    """

    if unreported:
        return Lifecycle.UNREPORTED
    if refused:
        return Lifecycle.REVOKED
    if found is None:
        return Lifecycle.CONFIGURED
    if stale:
        return Lifecycle.STALE
    return Lifecycle.UNAUTHORIZED if authority == "missing" else found
