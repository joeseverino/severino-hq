"""When the controller should sweep, and how it hears that there is work.

Applying a queued operation should happen the moment it is asked for, and
sweeping records that change monthly should not, so the two run on different
clocks. HQ records when each provider was last swept and answers "is one
due?"; the controller pulls and executes, so the web process holds no provider
credential and a compromise of it reaches no provider and opens no shell.
"""

import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, F, Max, OuterRef, Q
from django.utils import timezone
from django.utils.text import get_text_list
from pydantic import Field, GetJsonSchemaHandler
from pydantic_core import CoreSchema

from hq.domains.control_plane.models import (
    OPERATION_LAPSE,
    DashboardRefreshRequest,
    ManagedResource,
    OperationRequest,
    ProviderConnection,
    ProviderInventory,
    ReadRequest,
    attempted_at,
)
lazy from hq.domains.control_plane.bridge_contract import declared_names
lazy from hq.domains.control_plane.connection_kinds import LOGS_IN
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.observations.github import REPOSITORY_KIND
lazy from hq.domains.control_plane.provider_adapters.github import KIND as GITHUB_KIND
lazy from hq.domains.control_plane.providers import PROVIDERS, enabled_controller_actions, registry_label
lazy from hq.platform.core.attribution import queued_in
lazy from hq.platform.core.doorbell import ask_soon

from . import readings
from .action_links import READ_NOW_CAPABILITY
from .asks import Asked, AskedAnswer, Standing, asked_read
from .command_fields import Choices, Describe, Names
from .contracts import Effect
from .derivations import asked_ahead, holds_until, passed, present, since
from .integration_specs import CapabilitySpec, requested
from .labels import sentence
from .moments import duration
from .projection import read_once
from .read_requests import ForcedRead, connection_providers, due_reads, forced_kinds, forced_reads
from .record_errors import Sentence
from .refusal_codes import Code
from .security import Capability, Principal
from .tone import Absent, Tone, Toned
from .ui import ended
lazy from . import outbound_work
lazy from .adoption import manages_through, observes_only
lazy from .automation_runs import READ_PROFILE, claimant
lazy from .facts import stored_snapshots
lazy from .inbound_events import covers

# A kind HQ stores readings of. Read as a schema is first built, so declaring
# the command loads no registry.
type SweptKind = Annotated[str, Choices([(kind, registry_label(kind)) for kind in sorted({*OBSERVATIONS, *PROVIDERS})])]


@dataclass(frozen=True, slots=True)
class ControllerSweepCommand:
    """A wake-up request; with subjects, those are read on the next pull.

    Each of ``connection_refs`` forces every kind its credential reads, each of
    ``kinds`` that kind, and ``every_connection`` the whole sweep, alone. A
    page's Refresh names every subject its problems rest on, in one call.
    """

    connection_refs: Annotated[
        tuple[str, ...],
        Names("connections"),
        Field(title="Connections"),
        Describe("The connections to read now, every type each one's credential reads."),
    ] = ()
    kinds: Annotated[
        tuple[SweptKind, ...],
        Names("readings"),
        Field(title="Types of reading"),
        Describe("The types of reading to read now, from every connection."),
    ] = ()
    every_connection: Annotated[bool, Describe("Read every connection now.")] = False

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        # The shapes a sweep takes, said beside its fields: none, every
        # connection alone, or some connections and types.
        declared: dict[str, Any] = handler(schema)
        handler.resolve_ref_schema(declared)["oneOf"] = _SHAPES
        return declared


_NONE: dict[str, dict[str, Any]] = {
    "connection_refs": {"maxItems": 0},
    "kinds": {"maxItems": 0},
    "every_connection": {"const": False},
}
_SHAPES: list[dict[str, Any]] = [
    {"properties": _NONE},
    {"properties": {**_NONE, "every_connection": {"const": True}}, "required": ["every_connection"]},
    {
        "properties": {"every_connection": {"const": False}},
        "anyOf": [
            {"properties": {field: {"minItems": 1}}, "required": [field]} for field in ("connection_refs", "kinds")
        ],
    },
]


def _seconds(name: str) -> int:
    return max(0, int(getattr(settings, name)))


def _path(name: str, filename: str) -> Path:
    configured = getattr(settings, name).strip()
    if configured:
        return Path(configured)
    # Beside the database, which is the volume both sides share. Anywhere else
    # is visible in the container and invisible from the host, which is the one
    # place the watcher runs.
    return Path(settings.DATABASES["default"]["NAME"]).parent / filename


def note_activity() -> None:
    """Record that somebody is using HQ, cheaply enough to do on every request.

    A file's mtime rather than a column, rewritten at most once per interval,
    so the common case is a stat. Best effort: nothing an operator asked for
    may fail because a hint about scheduling could not be written.
    """

    marker = _path("SEVERINO_ACTIVITY_MARKER", "hq-activity")
    throttle = _seconds("SEVERINO_ACTIVITY_THROTTLE_SECONDS")
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < throttle:
            return
        _touch(marker)
    except OSError:
        return


def recently_used(now: float | None = None) -> bool:
    """Whether HQ has been used inside the active window."""

    if asked_ahead():
        # Asked on behalf of the next request, which is itself use.
        return True
    marker = _path("SEVERINO_ACTIVITY_MARKER", "hq-activity")
    window = _seconds("SEVERINO_ACTIVE_WINDOW_SECONDS")
    try:
        touched = marker.stat().st_mtime
    except OSError:
        return False
    if (time.time() if now is None else now) - touched > window:
        return False
    holds_until(datetime.fromtimestamp(touched + window, tz=UTC))
    return True


def note_controller() -> None:
    """Record that the controller reached HQ, on the call every real run makes.

    A file's mtime, like the activity marker, written once a pass. It shows
    what no sweep record can: a controller that stopped arriving, which
    otherwise looks like a quiet estate. Best effort, so the controller's work
    never fails because of it.
    """

    try:
        _touch(_path("SEVERINO_CONTROLLER_HEARTBEAT", "controller-heartbeat"))
    except OSError:
        return


# The host also starts a pass this long after the last one ended, to catch a
# doorbell rung while the host was off, a claim whose controller died, and the
# day's automatic work. deploy/systemd/severino-hq-controller.timer must hold
# the same value (tested).
CONTROLLER_FLOOR = timedelta(minutes=15)

# A healthy controller arrives at least once per floor; the second floor allows
# for a long pass.
CONTROLLER_SILENT_AFTER = 2 * CONTROLLER_FLOOR


# What each worker that pulls HQ's work is called in a sentence about it.
CONTROLLER_NAMED = "controller"
PLAYBOOK_WORKER_NAMED = "playbook worker"


class WorkerState(Toned):
    """Whether a worker that pulls HQ's work arrives, as its one state says it."""

    ONLINE = "online", Tone.GOOD
    SILENT = "silent", Tone.ATTENTION
    # Never run is known to be neither online nor silent.
    NOT_CONFIGURED = Absent.NOT_SET_UP
    # Never heard from, though it is always there: one that speaks only when
    # something happens.
    UNHEARD = "nothing yet", Tone.NEUTRAL


@dataclass(frozen=True, slots=True)
class WorkerStanding:
    """When a worker that pulls HQ's work (the controller, or HQ's playbook
    worker) last reached HQ, and whether that is too long ago."""

    seen_at: datetime | None
    silent: bool
    # Seen by its heartbeat, written only as it reaches this HQ, rather than
    # by readings that may have come with a database copied from elsewhere.
    here: bool = False
    named: Sentence = CONTROLLER_NAMED
    # What never heard from means of it.
    unheard: WorkerState = WorkerState.NOT_CONFIGURED
    # When it last took work, for a worker that claims it.
    claimed_at: datetime | None = None

    @property
    def known(self) -> bool:
        return self.seen_at is not None

    @property
    def state(self) -> WorkerState:
        if self.silent:
            return WorkerState.SILENT
        return WorkerState.ONLINE if self.known else self.unheard

    @property
    def takes_reads(self) -> bool:
        """Whether a read asked for now will be read: a controller reaches
        this HQ, and has lately. Readings alone never say so, since a copy of
        another host's database holds them with no controller to drain its
        requests."""

        return self.here and not self.silent

    def cannot(self, doing: Sentence, *, now: datetime) -> Sentence:
        """Why work asked of it now would wait for nothing, as the sentence
        a refusal says ("Cannot refresh: no controller reports to this HQ."),
        or, with ``doing`` empty, what work already queued for it waits for:
        nothing expires a queued run, so it starts once one reports ("Starts
        when a playbook worker reports; 3 hours since one last did."). "" while it
        reports. The one wording of an absent or late worker, whichever
        worker it is."""

        if self.seen_at is None or not self.here:
            late = ""
        elif self.silent:
            late = f"{duration(since(self.seen_at, now=now))}"
        else:
            return ""
        if not doing:
            return (
                f"Starts when a {self.named} reports; {late} since one last did."
                if late
                else f"Starts when a {self.named} reports; none ever has."
            )
        return (
            f"Cannot {doing}: {late} since a {self.named} last reported."
            if late
            else f"Cannot {doing}: no {self.named} reports to this HQ."
        )


def controller_standing(now: datetime | None = None) -> WorkerStanding:
    """The controller's last arrival: its heartbeat, else the newest read it
    reported, answered or failed, since only a controller writes one (a
    database copied from another host has readings and no heartbeat here).
    Never seen is unknown, not silent: an installation with no controller has
    nothing to be late."""

    marker = _path("SEVERINO_CONTROLLER_HEARTBEAT", "controller-heartbeat")
    seen_at: datetime | None
    try:
        seen_at, here = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.get_current_timezone()), True
    except OSError:
        seen_at, here = newest_reading(), False
    if seen_at is None:
        return WorkerStanding(seen_at=None, silent=False)
    silent = (now or timezone.now()) - seen_at > CONTROLLER_SILENT_AFTER
    return WorkerStanding(seen_at=seen_at, silent=silent, here=here)


# HQ's playbook workers' arrivals, one per privilege profile, each a reading
# rather than a file beside the controller's: the findings and the queue that
# say one is absent are derived from rows (``derivations``), so a row is what
# moves them when it arrives.
AUTOMATION = "automation"
# The host starts the workers this long after the last ended, as well as at
# its doorbell. deploy/systemd/severino-hq-automation.timer must hold the same
# value (tested).
AUTOMATION_FLOOR = timedelta(minutes=15)
# A worker that arrives arrives at least once per floor; the second floor
# allows for one that started late. One holding a run renews its lease instead.
AUTOMATION_SILENT_AFTER = 2 * AUTOMATION_FLOOR
# A worker claims until nothing waits, so one pass claims several times, and
# a write a minute says all an age is worded in.
_AUTOMATION_NOTED_EVERY = timedelta(minutes=1)
# The work only HQ's playbook workers claim.
AUTOMATION_WORK = (OperationRequest.Action.SCAN, OperationRequest.Action.RUN)


def _automation(profile: str) -> str:
    """What the arrival of the worker lent ``profile``'s logins is noted as (``readings.note``)."""

    return f"{AUTOMATION}.{profile}"


def automation_heartbeat(profile: str) -> str:
    """The reading that holds when the worker lent ``profile``'s logins last arrived."""

    return readings.heartbeat(_automation(profile))


def note_automation(profile: str) -> None:
    """Record that the playbook worker lent ``profile``'s logins reached HQ,
    as each of its claims arrives, so a worker that finds nothing to take
    still counts as seen.

    Written only by the bridge's claim over the root-only socket, never by a
    page. It holds a moment and nothing else, so it names no secret.
    """

    readings.note(_automation(profile), every=_AUTOMATION_NOTED_EVERY)


def _automation_claims() -> dict[str, tuple[datetime | None, datetime | None]]:
    """Each profile's worker's newest claim, and the last of its live leases
    to run out, read with every other profile's in one query a projection."""

    def load() -> dict[str, tuple[datetime | None, datetime | None]]:
        found: dict[str, tuple[datetime | None, datetime | None]] = {}
        rows = (
            OperationRequest.objects.filter(action__in=AUTOMATION_WORK)
            .values("action", "resource__spec__privilege")
            .annotate(
                newest=Max("claimed_at"),
                leased=Max("lease_expires_at", filter=Q(state=OperationRequest.State.CLAIMED)),
            )
        )
        for row in rows:
            profile = claimant(row["action"], row["resource__spec__privilege"])
            held = found.get(profile, (None, None))
            found[profile] = (_later(held[0], row["newest"]), _later(held[1], row["leased"]))
        return found

    return read_once("cadence.automation_claims", load)


def _later(one: datetime | None, other: datetime | None) -> datetime | None:
    return max((moment for moment in (one, other) if moment is not None), default=None)


def automation_profiles() -> tuple[str, ...]:
    """Every profile HQ has asked a scan or run of, the read profile's first."""

    return (READ_PROFILE, *sorted(set(_automation_claims()) - {READ_PROFILE}))


def automation_standing(profile: str) -> WorkerStanding:
    """The last arrival of the playbook worker lent ``profile``'s logins: its
    heartbeat, or its newest claim of the scans or runs it takes. Silent once
    two of its timer's floors pass with no arrival, unless it still holds a
    run's lease. Unlike the controller, one never seen is absent: a scan or
    run asked of it waits for nothing, so each is refused while it stands
    (``cannot``)."""

    now = present()
    # The read profile's worker always runs once automation is set up; another
    # runs once a connection lends its profile a login.
    named, unheard = (
        (PLAYBOOK_WORKER_NAMED, WorkerState.NOT_CONFIGURED)
        if profile == READ_PROFILE
        else (f"{PLAYBOOK_WORKER_NAMED} for the {profile} profile", WorkerState.UNHEARD)
    )
    claimed, leased = _automation_claims().get(profile, (None, None))
    seen = [moment for moment in (readings.heard(_automation(profile)), claimed) if moment is not None]
    if not seen:
        return WorkerStanding(seen_at=None, silent=False, named=named, unheard=unheard)
    seen_at = max(seen)
    holding = leased is not None and not passed(leased, now=now)
    silent = not holding and passed(seen_at + AUTOMATION_SILENT_AFTER, now=now)
    return WorkerStanding(seen_at=seen_at, silent=silent, here=True, named=named, claimed_at=claimed)


def newest_reading() -> datetime | None:
    """When a controller last reported a read, whichever controller and
    whether or not it answered: a failed first read stores no ``observed_at``
    and is still a report."""

    times = [attempted_at(row.observed_at, row.updated_at) for rows in stored_snapshots().values() for row in rows]
    return max(times, default=None)


def sweep_interval() -> timedelta:
    """How stale a sweep may be before another is worth the calls.

    Adaptive, because the answer depends on whether anybody is looking. In use,
    a view of the estate a few minutes old is the point of having one, and the
    interval stays longer than a sweep takes so the controller is idle, and
    answers its doorbell, between them. Idle, twelve hours of staleness costs
    nothing and saves the calls.
    """

    active = _seconds("SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS")
    idle = _seconds("SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS")
    return timedelta(seconds=active if recently_used() else idle)


def slowest_sweep_interval() -> timedelta:
    """The longest gap between sweeps the policy permits, whoever is watching.

    The ceiling rather than the current value, for anything that needs a stable
    number: a staleness threshold derived from the live interval moves with
    the thing it is measuring.
    """

    return timedelta(seconds=_seconds("SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS"))


def ssh_probe_interval() -> timedelta:
    """How long a working SSH connection's last answer stays good enough.

    A probe of an SSH connection is a real login: a certificate minted or a key
    offered, a session opened, a command run. That is fine twice a day and not
    fine every minute, which is what the active sweep cadence would make it,
    and a shared host counts those logins, and may act on them. Everything else
    a sweep reads is cheap enough to keep on the sweep's own clock.
    """

    return timedelta(seconds=_seconds("SEVERINO_SSH_PROBE_INTERVAL_SECONDS"))


def carried_connections(controller_id: str, *, failing: bool = False) -> list[str]:
    """Connections that log in to a machine (``ConnectionKind.logs_in``)
    this controller should report without probing again.

    Only ones whose last probe succeeded within ``ssh_probe_interval``, so a
    recovery shows as soon as it happens. ``failing`` carries the failing ones
    too, for a sweep run only because somebody asked for a reading: a host
    refusing a login counts every further attempt, and opening pages must not
    become a login every few minutes against it.
    """

    fresh_since = timezone.now() - ssh_probe_interval()
    probes = ProviderConnection.objects.filter(
        controller_id=controller_id,
        provider__in=LOGS_IN,
        probed=True,
        observed_at__gte=fresh_since,
    )
    if not failing:
        probes = probes.filter(reachable=True)
    return sorted(probes.values_list("connection_ref", flat=True))


def swept(kind: str) -> bool:
    """Whether the controller's sweep is what reads ``kind``.

    The one place that says so: the sweep's own clock and a page asking for a
    reading both go by it. A kind nothing declares is not swept.
    """

    reading = OBSERVATIONS.get(kind)
    if reading is not None:
        return reading.read_by == "controller"
    declared = PROVIDERS.get(kind)
    return declared is not None and declared.sweeps


def own_clock(kind: str) -> timedelta | None:
    """How often ``kind`` is read on a clock of its own, slower than the
    sweep's, or None for a kind every sweep reads: its declaration's, else the
    idle sweep's while events keep it current (``inbound_events.covers``),
    which returns to every sweep on its own once their channel goes silent."""

    reading = OBSERVATIONS.get(kind)
    if reading is not None and reading.every is not None:
        return reading.every
    return slowest_sweep_interval() if swept(kind) and covers(kind) else None


def every_sweep(kind: str) -> bool:
    """Whether every sweep reads ``kind``: swept, and on no slower clock of its
    own. A kind on its own clock is carried by the sweeps between its reads, so
    its age says nothing about whether a sweep is due or has happened."""

    return swept(kind) and own_clock(kind) is None


def sweep_due(controller_id: str = "") -> dict[str, object]:
    """Whether the controller should sweep now, and why.

    The oldest sweep decides, unless a pending ``ReadRequest`` makes it due
    whatever the cadence says. The reason rides along because a controller
    that stopped sweeping and one told not to look are the same from outside,
    and only one of them is a fault.
    """

    forced = due_reads(forced_reads())
    interval = sweep_interval()
    oldest = _oldest_sweep()
    verdict: dict[str, object] = {
        "ok": True,
        "interval_seconds": int(interval.total_seconds()),
        # Connections the sweep reports as they last were rather than probes.
        "carry": _carry(controller_id, forced),
        "forced": [read.as_dict() for read in forced],
        # When not empty, every kind the sweep reads: set when the cadence is
        # not due and every pending request names its kinds.
        "only_kinds": [],
        "carry_kinds": _carry_kinds(forced),
    }
    if oldest is None:
        return {**verdict, "due": True, "reason": ended(sentence(Absent.NOT_READ))}
    age = timezone.now() - oldest
    due = age >= interval
    reason = (
        f"The oldest reading is {duration(age)} old and everything is read "
        f"every {duration(interval)}: {'due' if due else 'not due'}."
    )
    if not due and forced:
        reason += ended(f" A refresh was asked for {', '.join(read.subject for read in forced)}")
        # Due only because somebody asked: SSH is left as it was last found,
        # working or not, unless it is what they asked for.
        verdict["carry"] = _carry(controller_id, forced, failing=True)
        verdict["only_kinds"] = _asked_kinds(forced)
    return {
        **verdict,
        "due": due or bool(forced),
        "reason": reason,
        "age_seconds": int(age.total_seconds()),
    }


def _oldest_sweep() -> datetime | None:
    """Of what the sweep reads, when the kind it tried longest ago was tried.

    A kind HQ reads itself (a public registry) is stored beside these and is
    not the sweep's to refresh: counted here, its age would make every run a
    sweep. Tried, not read: a provider that is down has been tried, and is
    tried again at the next interval rather than on every run.
    """

    rows = ProviderInventory.objects.values_list("kind", "observed_at", "updated_at")
    return min(
        (attempted_at(observed_at, updated_at) for kind, observed_at, updated_at in rows if every_sweep(kind)),
        default=None,
    )


def _carry_kinds(forced: tuple[ForcedRead, ...]) -> list[str]:
    """The kinds events keep current that the sweep reports as they last
    were: neither due on their own clock nor asked for. A controller's kind
    with a clock of its declaration's is carried by its own plan."""

    if any(read.kinds is None for read in forced):
        return []
    asked = {kind for read in forced for kind in read.kinds or ()}
    return sorted(
        kind
        for kind, observed_at, updated_at in ProviderInventory.objects.values_list("kind", "observed_at", "updated_at")
        if kind not in asked
        and covers(kind)
        and (every := own_clock(kind)) is not None
        and not passed(attempted_at(observed_at, updated_at) + every)
    )


def _carry(controller_id: str, forced: tuple[ForcedRead, ...], *, failing: bool = False) -> list[str]:
    """The connections a controller's sweep reports as last found: none
    without a controller, and never one somebody asked to read."""

    if not controller_id:
        return []
    asked = {read.connection_ref for read in forced if read.connection_ref}
    return [ref for ref in carried_connections(controller_id, failing=failing) if ref not in asked]


def _asked_kinds(forced: tuple[ForcedRead, ...]) -> list[str]:
    """Every kind the asked-for reads force, or none to say every kind, when
    one of them asked for everything."""

    if any(read.kinds is None for read in forced):
        return []
    return sorted({kind for read in forced for kind in read.kinds or ()})


type Subjects = tuple[tuple[str, ...], tuple[str, ...]]


def _read_subject(command: ControllerSweepCommand) -> Subjects | None:
    """``(connection_refs, kinds)`` the command forces, both empty for every
    connection, or None for a bare wake-up.

    Fails closed: a ref no controller reported. The command's type holds each
    kind to one HQ stores.
    """

    refs = tuple(dict.fromkeys(ref.strip() for ref in command.connection_refs if ref.strip()))
    kinds = tuple(dict.fromkeys(command.kinds))
    if command.every_connection and (refs or kinds):
        raise ValidationError({"every_connection": "Name connections and types of reading, or every connection."})
    if unknown := [ref for ref in refs if not connection_providers(ref)]:
        raise ValidationError(
            {"connection_refs": ValidationError(f"No such connection: {', '.join(unknown)}.", code=Code.NOT_FOUND)}
        )
    return (refs, kinds) if refs or kinds or command.every_connection else None


def pass_due() -> str:
    """Why the controller should run a pass now, or "" when a pass would find nothing.

    The doorbell (`hq.platform.core.doorbell`) asks this once a minute, and
    again as each capability's write commits: a capability's change starts a
    pass at once, any other writer's within the minute. A dashboard reading
    is the glance pass's, on its own doorbell, so it starts no pass here.
    """

    now = timezone.now()
    if OperationRequest.objects.filter(state=OperationRequest.State.QUEUED).exists():
        return "Work is queued."
    if OperationRequest.objects.filter(OPERATION_LAPSE.q(now)).exists():
        return "Work a controller took did not finish in time."
    if unapplied():
        return "A record's settings changed and nothing has applied them yet."
    verdict = sweep_due()
    return str(verdict["reason"]) if verdict["due"] else ""


RECONCILE = OperationRequest.Action.RECONCILE


def unapplied() -> list[str]:
    """Keys of records the controller applies on its own whose generation moved
    with nothing queued or run for it that applies the new generation.

    Whoever moved a generation (an edit, an upload, a target that changed what
    others resolve to), this is what notices, so no writer queues the apply.
    """

    automatic = enabled_controller_actions(automatic_only=True)
    kinds = {kind for kind, action in automatic if action == RECONCILE}
    # The schedule queues one of a record's automatic actions at a time (a
    # certificate in its renewal window gets a renewal, not a reconcile), and
    # whichever it is applies the generation it records. So one that failed is
    # not asked for again each minute: the controller's schedule retries it.
    applies = {RECONCILE} | {action for kind, action in automatic if kind in kinds}
    handled = OperationRequest.objects.filter(
        resource=OuterRef("pk"), action__in=applies, input__generation=OuterRef("generation")
    )
    # One query whatever the estate holds; only the rows it returns, normally
    # none, are read further.
    moved = list(
        ManagedResource.objects.filter(enabled=True, kind__in=kinds, generation__gt=F("observed_generation"))
        .exclude(Exists(handled))
        .values_list("key", "kind", "spec")
    )
    if not moved:
        return []
    # The controller's schedule skips a record it may only read, so a pass
    # would find nothing to do for it.
    manages = manages_through()
    return [key for key, kind, spec in moved if not observes_only(kind, spec, manages)]


def ring_when_due() -> bool:
    """Ring the controller if a pass is due; whether it rang. Asked by the
    doorbell, once a minute and as an ask commits, so `pass_due` alone decides."""

    return bool(pass_due()) and _ring("controller")


# Who is asked: the controller (its pass), the glance pass that reads only the
# dashboard's panels, so a refresh never waits behind a sweep under way, and
# HQ's playbook worker, which claims a run over the bridge.
type Doorbell = Literal["controller", "glance", "automation"]

# Each doorbell's file beside the controller's, which is the one HQ is told of
# (empty: that file itself); the unit on the host that starts each worker
# waits for its file.
DOORBELLS: dict[Doorbell, str] = {
    "controller": "",
    "glance": "glance-doorbell",
    "automation": "automation-doorbell",
}


def _ring(doorbell: Doorbell) -> bool:
    """Touch ``doorbell``'s file, telling the host there is work and nothing else.

    The worker it starts pulls the work its usual way, so forged, deleted or
    replayed, the worst a ring causes is a run that finds nothing. Best effort:
    asking never depends on the host filesystem.
    """

    path = _path("SEVERINO_CONTROLLER_DOORBELL", "controller-doorbell")
    try:
        _touch(path.with_name(DOORBELLS[doorbell]) if DOORBELLS[doorbell] else path)
    except OSError:
        return False
    return True


def _ring_controller() -> None:
    # On the doorbell's thread, so whether a pass is due is asked off the
    # request, and asks that commit together are one ask.
    ask_soon()


def _ring_glance() -> None:
    _ring("glance")


def _ring_automation() -> None:
    _ring("automation")


_RINGS: dict[Doorbell, Callable[[], None]] = {
    "controller": _ring_controller,
    "glance": _ring_glance,
    "automation": _ring_automation,
}


# The doorbells the capability running now has asked for, which the runner
# rings once each as it returns (``ringing``); None outside one.
_asked: ContextVar[set[Doorbell] | None] = ContextVar("asked_doorbells", default=None)


@contextmanager
def ringing() -> Iterator[set[Doorbell]]:
    """Ring each doorbell the work inside asks for once, as it returns.

    Around every capability, so a handler's asks are one ring, rung once they
    commit so the worker never arrives to find nothing. Work that raised rings
    nothing.
    """

    if (asked := _asked.get()) is not None:
        yield asked
        return
    asked = set()
    with _asked.set(asked):
        yield asked
    for doorbell in sorted(asked):
        transaction.on_commit(_RINGS[doorbell], robust=True)


def _ring_on_commit(doorbell: Doorbell) -> None:
    if (asked := _asked.get()) is not None:
        asked.add(doorbell)
    else:
        # Outside a capability (the boot, the controller's report): rung as
        # the caller's transaction commits, or at once outside one.
        transaction.on_commit(_RINGS[doorbell], robust=True)


# Where the boot keeps each reading kind's ``ObservationSpec.fingerprint`` as
# of the last release that booted.
RECORD_CONTRACTS = "record-contracts"


def request_boot_reads() -> tuple[str, ...]:
    """Ask for what a new image needs read before HQ's readings are current; the kinds asked.

    A deploy moves which commit each extension runs and what the repositories'
    deployments say, and a kind whose fingerprint moved holds readings let
    through under another contract. Asked by the boot, so the idle cadence is
    untouched.
    """

    current = {kind: spec.fingerprint(declared_names()) for kind, spec in OBSERVATIONS.items()}
    reading = readings.stored(RECORD_CONTRACTS)
    stored = reading.value if reading is not None and isinstance(reading.value, dict) else {}
    held = set(ProviderInventory.objects.values_list("kind", flat=True))
    delivery = GITHUB_KIND in held or ManagedResource.objects.filter(kind=GITHUB_KIND).exists()
    moved = sorted(kind for kind in held if kind in current and stored.get(kind) != current[kind] and swept(kind))
    deployed = (REPOSITORY_KIND,) if REPOSITORY_KIND in held else ()
    asked = tuple(dict.fromkeys(((GITHUB_KIND,) if delivery else ()) + deployed + tuple(moved)))
    if asked:
        ask(reads=asked)
    readings.record(RECORD_CONTRACTS, current)
    return asked


@transaction.atomic
def ask(
    *,
    reads: Iterable[str] = (),
    connection: str = "",
    every: bool = False,
    panels: Mapping[str, str] | None = None,
    doorbell: Doorbell = "controller",
    activity: bool = False,
    spaced: bool = False,
) -> datetime:
    """Ask a worker outside this process for work, the one way HQ does: store
    what is asked, in the caller's transaction, and ring ``doorbell`` once
    it commits. When it was asked.

    ``activity`` notes that a person asked, so the active cadence decides the
    sweep and a panel says its reading is under way. ``spaced`` is an
    event's ask, which waits a gap after its kind was stored
    (``read_requests.EVENT_READ_GAP``). A kind HQ reads itself starts the
    host job that declares it (``OutboundWork.reads``) once this commits
    instead, which reads what of it is due. Any other kind HQ stores no
    reading of is refused, since nothing would answer it and it
    would force sweeps until it expired.
    """

    now = timezone.now()
    kinds = tuple(dict.fromkeys(reads))
    read_here = {kind: work for kind in kinds if (work := outbound_work.reader_of(kind))}
    if unknown := [
        kind for kind in kinds if kind not in OBSERVATIONS and kind not in PROVIDERS and kind not in read_here
    ]:
        raise ValidationError({"kind": ValidationError(f"No such type: {', '.join(unknown)}.", code="invalid_choice")})
    if read_here:
        kinds = tuple(kind for kind in kinds if kind not in read_here)
        for work in dict.fromkeys(read_here.values()):
            transaction.on_commit(partial(outbound_work.start_reading, work), robust=True)
    if activity:
        note_activity()
    for kind in kinds or (("",) if connection or every else ()):
        ReadRequest.objects.update_or_create(
            connection_ref=connection,
            kind=kind,
            defaults={"requested_at": now, "traceparent": queued_in(), "spaced": spaced},
        )
    if panels:
        asked = DashboardRefreshRequest.objects.select_for_update().in_bulk(list(panels), field_name="panel_id")
        for panel_id, said in panels.items():
            request = asked.get(panel_id) or DashboardRefreshRequest(panel_id=panel_id)
            request.requested_at, request.completed_at, request.awaited = now, None, activity
            request.audit_said = said
            request.save()
    if kinds or connection or every or panels or not read_here:
        _ring_on_commit(doorbell)
    return now


class ControllerSweepRequested(AskedAnswer):
    """The controller, woken to read the estate, and why a full read is due or not."""

    due: Annotated[bool, Describe("Whether a read of every connection was due.")]
    reason: Annotated[str, Describe("Why a full read is due or not, in a sentence.")]
    read_now: Annotated[
        str,
        Describe("What the controller reads at once, by its connection or kind; empty when the call named nothing."),
    ]


def request_controller_sweep(
    command: ControllerSweepCommand,
    *,
    principal: Principal,
) -> ControllerSweepRequested:
    """Wake the pull-based controller; with subjects, have it read those now."""

    del principal
    subject = _read_subject(command)
    refs, kinds = subject or ((), ())
    # Noted before policy is read, so the active cadence (not the twelve-hour
    # idle economy) decides the sweep.
    asks = [ask(connection=ref, activity=True) for ref in refs]
    if kinds or command.every_connection or not refs:
        asks.append(ask(reads=kinds, every=command.every_connection, activity=True))
    at = min(asks)
    verdict = sweep_due()
    said = _sweep_message(subject, due=bool(verdict["due"]))
    # A bare wake-up names nothing to wait for; every connection waits on every kind.
    forced = (*kinds, *(kind for ref in refs for kind in forced_kinds(ref, "") or ()))
    asked = (
        Asked(Standing(note=said), message=said)
        if subject is None
        else asked_read(
            dict.fromkeys(forced), at, connection_ref=refs[0] if len(refs) == 1 and not kinds else "", message=said
        )
    )
    return {
        **asked.answer(),
        "due": bool(verdict["due"]),
        "reason": str(verdict["reason"]),
        "read_now": _read_label(subject),
    }


# Named, so prose that tells a person which button to press reads its words.
CONTROLLER_REFRESH = CapabilitySpec(
    READ_NOW_CAPABILITY,
    "Ask HQ to read everything again now.",
    Effect.READ_REQUEST,
    Capability.MANAGE_INFRASTRUCTURE,
    ControllerSweepCommand,
    request_controller_sweep,
    execution_notes=(
        "Mark HQ as in use, so readings are taken more often.",
        "Wake the controller. HQ's web process holds no credential for any service.",
        "The controller reads what HQ says is due, with its own credentials.",
    ),
    label="Refresh HQ's readings",
    verb="Refresh",
    variants=(("all", "Refresh all"),),
)


def _read_label(subject: Subjects | None) -> str:
    if subject is None:
        return ""
    refs, kinds = subject
    return ", ".join((*refs, *kinds)) or "every connection"


def _sweep_message(subject: Subjects | None, *, due: bool) -> str:
    """What asking says, in the refresh command's own words."""

    refs, kinds = subject or ((), ())
    if refs or kinds:
        return CONTROLLER_REFRESH.requested(get_text_list([*refs, *map(registry_label, kinds)], "and"))
    asked = requested(CONTROLLER_REFRESH.words("all"))
    return asked if due or subject is not None else f"{asked} What HQ shows is already up to date."


def _touch(path: Path) -> None:
    """Replace a marker, so a watcher sees an event it cannot coalesce away.

    `Path.touch` on an existing file is a bare utime, which inotify may fold
    into nothing. A replacement is a create, and `os.replace` is atomic, so a
    reader never catches the file absent.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".marker-")
    os.close(handle)
    Path(temporary).replace(path)
