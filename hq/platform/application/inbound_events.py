"""What HQ knows of the events it was told of and the ones it takes: each
channel's standing, and what each declared event makes due.

The reading side of ``inbound``, which alone writes them, so a page or a
finding reads here without being able to say an event happened.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from hq.domains.control_plane.models import InboundSource
from hq.domains.control_plane.observations.cloudflare import WORKER_KIND
lazy from hq.domains.control_plane import inbound_worker
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.provider_adapters import INBOUND
lazy from hq.domains.control_plane.provider_spec import InboundSpec

from . import readings
from .derivations import passed
from .moments import moment
from .projection import read_once
lazy from .cadence import WorkerStanding, WorkerState
lazy from .connection_setup import docs_url
lazy from .credential_sight import standing
lazy from .facts import stored_snapshots

if TYPE_CHECKING:
    from .credential_sight import Standing

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


@dataclass(frozen=True, slots=True)
class EventsWorker:
    """The events Worker as Cloudflare runs it, from the ``cloudflare.worker``
    reading: the deployment serving it and its last day of invocations, none
    of either where the reading holds none."""

    standing: Standing
    connection_ref: str = ""
    deployed_at: datetime | None = None
    # (number, tag) of each version the deployment serves.
    versions: tuple[tuple[int | None, str], ...] = ()
    requests: int | None = None
    errors: int | None = None
    # CPU time per invocation, in milliseconds.
    cpu_time_p50_ms: float | None = None
    cpu_time_p99_ms: float | None = None

    @property
    def deployed(self) -> bool:
        return bool(self.versions)

    @property
    def behind(self) -> bool:
        """Whether a version it serves was deployed from other routes or
        configuration than HQ generates: tagged otherwise, or not at all."""

        tags = {tag for _number, tag in self.versions}
        return bool(tags) and ("" in tags or tags != {inbound_worker.deploy_tag()})


def events_worker() -> EventsWorker:
    """The events Worker, by the name its configuration gives it."""

    return read_once("inbound_events.worker", _events_worker)


def _events_worker() -> EventsWorker:
    row = next(iter(stored_snapshots().get(WORKER_KIND, ())), None)
    spec = OBSERVATIONS[WORKER_KIND]
    found = standing(WORKER_KIND, row, label=spec.label, permissions=tuple(spec.requires))
    name = inbound_worker.SETTINGS["name"]
    record: dict[str, Any] = next(
        (each for each in (row.records if row else ()) if isinstance(each, dict) and each.get("name") == name), {}
    )
    return EventsWorker(
        standing=found,
        connection_ref=str(record.get("connection_ref", "")),
        deployed_at=moment(record.get("deployed_at")),
        versions=tuple(
            (version.get("number"), str(version.get("tag", ""))) for version in record.get("versions") or ()
        ),
        requests=record.get("requests"),
        errors=record.get("errors"),
        cpu_time_p50_ms=_milliseconds(record.get("cpu_time_p50_us")),
        cpu_time_p99_ms=_milliseconds(record.get("cpu_time_p99_us")),
    )


def _milliseconds(micros: float | None) -> float | None:
    return None if micros is None else micros / 1000
