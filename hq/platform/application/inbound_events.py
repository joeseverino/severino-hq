"""What HQ knows of the events it was told of and the ones it takes: each
channel's standing, and what each declared event makes due.

The reading side of ``inbound``, which alone writes them, so a page or a
finding reads here without being able to say an event happened.
"""

from dataclasses import dataclass
from datetime import timedelta

from hq.domains.control_plane.models import InboundSource
lazy from hq.domains.control_plane.provider_adapters import INBOUND
lazy from hq.domains.control_plane.provider_spec import InboundSpec

from . import readings
from .derivations import passed
lazy from .cadence import WorkerStanding, WorkerState
lazy from .connection_setup import docs_url

# What each channel is called in a sentence about it.
LISTENER_NAMED = "event listener"

# How often each channel's reader (the Worker's listener, the Docker reader)
# says it is listening when nothing happens, as the contract's
# InboundHeartbeatSeconds, and so how long a channel may go unheard before it
# is silent: two of these.
CHANNEL_EVERY = timedelta(minutes=5)


def unheard(source: InboundSource) -> WorkerState:
    """What a channel never heard from is: the listener is not set up, while
    HQ's host jobs are always there and have had nothing to say."""

    return WorkerState.UNHEARD if source == InboundSource.HOST else WorkerState.NOT_CONFIGURED


def setup_guide(standing: WorkerStanding) -> str:
    """Where setting up the listener and the Worker it hears is written down,
    for a channel not set up; "" for any other."""

    if standing.state is not WorkerState.NOT_CONFIGURED:
        return ""
    return docs_url("Install.md", "12-hear-providers-events")


def channel(source: InboundSource) -> str:
    """What ``source``'s heartbeat is noted as (``readings.note``)."""

    return f"inbound-{source}"


def channel_standing(source: InboundSource) -> WorkerStanding:
    """When ``source`` last reported, and whether that is too long ago. A
    channel never heard is never silent (``unheard``)."""

    seen_at = readings.heard(channel(source))
    if seen_at is None:
        return WorkerStanding(seen_at=None, silent=False, named=LISTENER_NAMED, unheard=unheard(source))
    silent = passed(seen_at + 2 * CHANNEL_EVERY)
    return WorkerStanding(seen_at=seen_at, silent=silent, here=True, named=LISTENER_NAMED)


def covers(kind: str) -> bool:
    """Whether events keep ``kind`` current now: a declaration is complete
    for it and its channel was heard lately. A channel gone silent, or never
    heard, covers nothing, so its readings are swept as before."""

    for spec in INBOUND.values():
        if kind in spec.complete:
            standing = channel_standing(arrives_by(spec))
            if standing.seen_at is not None and not standing.silent:
                return True
    return False


def arrives_by(spec: InboundSpec) -> InboundSource:
    """The channel a declaration's events arrive by: the Worker routes each
    one with a path, and HQ's own host jobs tell the rest."""

    return InboundSource.WORKER if spec.path else InboundSource.HOST


@dataclass(frozen=True, slots=True)
class Taken:
    """Events one provider's declaration takes that make the same readings due."""

    provider: str
    source: InboundSource
    # Its channel's standing now.
    heard: WorkerState
    events: tuple[str, ...]
    reads: tuple[str, ...]
    # Those of ``reads`` the declaration keeps current while its channel is heard.
    kept: tuple[str, ...]


def taken() -> tuple[Taken, ...]:
    """Every event an admitted provider's declaration takes, its events
    gathered by the readings they make due, in the order each declares them."""

    heard = {source: channel_standing(source).state for source in InboundSource}
    found: list[Taken] = []
    for provider, spec in INBOUND.items():
        alike: dict[tuple[str, ...], list[str]] = {}
        for event, reads in spec.events.items():
            alike.setdefault(reads, []).append(event)
        source = arrives_by(spec)
        found += (
            Taken(provider, source, heard[source], tuple(events), reads, tuple(k for k in reads if k in spec.complete))
            for reads, events in alike.items()
        )
    return tuple(found)
