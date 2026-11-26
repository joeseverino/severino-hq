"""What HQ knows of the events it was told of: each channel's standing.

The reading side of ``inbound``, which alone writes them, so a page or a
finding reads here without being able to say an event happened.
"""

from datetime import timedelta

from hq.domains.control_plane.models import InboundSource

from . import readings
from .derivations import passed
lazy from .cadence import WorkerStanding

# What each channel is called in a sentence about it.
LISTENER_NAMED = "event listener"

# How often a channel says it is listening when nothing happens, and so how
# long it may go unheard before it is silent: two of these. HQ's own host jobs
# speak only as a unit fails, so their silence says nothing.
CHANNEL_EVERY: dict[InboundSource, timedelta | None] = {
    InboundSource.WORKER: timedelta(minutes=5),
    InboundSource.HOST: None,
}


def channel(source: InboundSource) -> str:
    """What ``source``'s heartbeat is noted as (``readings.note``)."""

    return f"inbound-{source}"


def channel_standing(source: InboundSource) -> WorkerStanding:
    """When ``source`` last reported, and whether that is too long ago. A
    channel never heard is not set up, never silent."""

    seen_at = readings.heard(channel(source))
    if seen_at is None:
        return WorkerStanding(seen_at=None, silent=False, named=LISTENER_NAMED)
    every = CHANNEL_EVERY[source]
    silent = every is not None and passed(seen_at + 2 * every)
    return WorkerStanding(seen_at=seen_at, silent=silent, here=True, named=LISTENER_NAMED)
