"""What HQ knows of the events it was told of: each channel's standing.

The reading side of ``inbound``, which alone writes them, so a page or a
finding reads here without being able to say an event happened.
"""

from datetime import timedelta

from hq.domains.control_plane.models import InboundSource

from . import readings
from .derivations import passed
lazy from .cadence import WorkerStanding, WorkerState
lazy from .connection_setup import docs_url

# What each channel is called in a sentence about it.
LISTENER_NAMED = "event listener"

# How often a channel says it is listening when nothing happens, and so how
# long it may go unheard before it is silent: two of these. HQ's own host jobs
# speak only as a unit fails, so their silence says nothing.
CHANNEL_EVERY: dict[InboundSource, timedelta | None] = {
    InboundSource.WORKER: timedelta(minutes=5),
    InboundSource.HOST: None,
}


def unheard(source: InboundSource) -> WorkerState:
    """What a channel never heard from is: the listener is not set up, while
    HQ's host jobs are always there and have had nothing to say."""

    return WorkerState.UNHEARD if source == InboundSource.HOST else WorkerState.NOT_CONFIGURED


def setup_guide(standing: WorkerStanding) -> str:
    """Where setting up the listener and the Worker it hears is written down,
    for a channel not set up; "" for any other."""

    if standing.state is not WorkerState.NOT_CONFIGURED:
        return ""
    return docs_url("Security.md", "the-inbound-events-worker")


def channel(source: InboundSource) -> str:
    """What ``source``'s heartbeat is noted as (``readings.note``)."""

    return f"inbound-{source}"


def channel_standing(source: InboundSource) -> WorkerStanding:
    """When ``source`` last reported, and whether that is too long ago. A
    channel never heard is never silent (``unheard``)."""

    seen_at = readings.heard(channel(source))
    if seen_at is None:
        return WorkerStanding(seen_at=None, silent=False, named=LISTENER_NAMED, unheard=unheard(source))
    every = CHANNEL_EVERY[source]
    silent = every is not None and passed(seen_at + 2 * every)
    return WorkerStanding(seen_at=seen_at, silent=silent, here=True, named=LISTENER_NAMED)
