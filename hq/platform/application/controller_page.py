"""The controller as HQ sees it: whether it is arriving, what it read, what waits for it.

Every reading on every page is the controller's, so "is it running, and what
did it last do" is a question HQ has to be able to answer in one place. All of
it is what HQ already holds: the heartbeat its bridge calls leave, the
readings it stored and when each was last attempted, the sweep policy, and
the work queued for it. Nothing here asks the controller anything.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
from django.utils.text import get_text_list

from hq.domains.control_plane.models import InboundSource, OperationRequest
from hq.domains.jobs.models import JobState

from .cadence import WorkerStanding, controller_standing, every_sweep, sweep_due
from .credential_sight import Standing
from .entity_links import EntityLink, entity_link, kind_label
from .freshness import freshness
from .moments import duration
from .read_requests import ForcedRead, forced_reads
from .references import Kind
from .tone import Tone
from .ui import counted
lazy from . import inbound_findings
lazy from .capabilities import capability_words
lazy from .findings import estate_findings, finding_key
lazy from .hq_context.sight_part import sight
lazy from .problems import About, problems_of
lazy from .security import Principal


@dataclass(frozen=True, slots=True)
class QueuedWork:
    """One thing waiting for the controller: a change to a record, or a refresh someone asked for."""

    record: EntityLink
    action: str
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ControllerPage:
    standing: WorkerStanding
    swept_at: datetime | None
    sweep_due: bool
    next_sweep_at: datetime | None
    # How often a sweep runs, in words; a reading on a clock of its own says
    # its clock on its row.
    sweep_every: str
    # Every reading HQ's page lists, which the cadence counts.
    readings: tuple[Standing, ...]
    # Of what every sweep reads, the reading tried longest ago: the one that
    # decides when the next sweep is due, and the one to look at when it is late.
    oldest: Standing | None
    # The oldest reading was last tried longer ago than its kind may age:
    # sweeps are not landing.
    overdue: bool
    queue: tuple[QueuedWork, ...]
    asked: tuple[QueuedWork, ...]

    @property
    def waiting(self) -> tuple[QueuedWork, ...]:
        return self.queue + self.asked

    @property
    def waiting_said(self) -> str:
        """What waits, counted in words, or "" when nothing does."""

        counts = ((len(self.queue), "change"), (len(self.asked), "refresh"))
        return get_text_list([counted(count, noun) for count, noun in counts if count], "and")

    @property
    def cadence(self) -> str:
        """How many of the readings every sweep reads, and how often."""

        every = sum(every_sweep(row.sight.kind) for row in self.readings)
        if every == len(self.readings):
            return f"Every reading is read every {self.sweep_every}"
        return f"{every:,} of {counted(len(self.readings), 'reading')} are read every {self.sweep_every}"

    @property
    def overdue_tone(self) -> str:
        return Tone.SERIOUS if self.overdue else ""

    @property
    def queue_tone(self) -> str:
        return Tone.ATTENTION if self.queue else ""


def controller_page(now: datetime | None = None) -> ControllerPage:
    now = now or timezone.now()
    verdict: dict[str, Any] = sweep_due()
    # Every reading tried, as HQ's page lists them, with when.
    readings = sight().rows
    tried = tuple((row, at) for row in readings if (at := row.sight.tried_at) is not None)
    age = verdict.get("age_seconds")
    interval = int(verdict.get("interval_seconds") or 0)
    due = bool(verdict.get("due"))
    queue = tuple(
        QueuedWork(
            record=entity_link("resource", operation.resource.key),
            action=operation.get_action_display(),
            state=operation.get_state_display(),
            created_at=operation.created_at,
        )
        for operation in OperationRequest.objects.filter(
            state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)
        )
        .select_related("resource")
        .order_by("created_at")[:50]
    )
    every = timedelta(seconds=interval)
    oldest = min(
        ((row, at) for row, at in tried if every_sweep(row.sight.kind)), key=lambda pair: pair[1], default=None
    )
    return ControllerPage(
        standing=controller_standing(now),
        swept_at=max((at for _row, at in tried), default=None),
        sweep_due=due,
        next_sweep_at=(None if due or age is None else now + timedelta(seconds=max(0, interval - int(age)))),
        sweep_every=duration(every),
        readings=readings,
        oldest=oldest[0] if oldest else None,
        overdue=oldest is not None and freshness(oldest[0].sight.kind, oldest[1], now).stale,
        queue=queue,
        asked=tuple(map(_refresh, forced_reads())),
    )


def _refresh(read: ForcedRead) -> QueuedWork:
    """An asked refresh as the work it waits as: what it reads, by name."""

    if read.connection_ref:
        record = entity_link("connection", read.connection_ref)
    elif read.kind:
        record = EntityLink(label=kind_label(read.kind), kind=read.kind, title=read.kind)
    else:
        record = EntityLink(label="Every connection")
    return QueuedWork(
        record=record,
        action=capability_words("infrastructure.controller.refresh"),
        state=JobState.QUEUED.label,
        created_at=read.requested_at,
    )


def controller_keys(found: Iterable[Any]) -> list[str]:
    """The keys of the findings about a controller, which has no page of its
    own, so a page finds its problems by them."""

    return [finding_key(finding) for finding in found if Kind.CONTROLLER.identity_in(finding.subject)]


def controller_problems(principal: Principal) -> tuple[dict[str, Any], ...]:
    """The open problems ``principal`` may read about the controller and the
    event listener its page shows beside it."""

    found = estate_findings(principal=principal)
    listener = [
        finding_key(finding)
        for finding in found
        if finding.rule == inbound_findings.RULE and finding.part == InboundSource.WORKER
    ]
    return problems_of(About(keys=[*controller_keys(found), *listener]), principal)
