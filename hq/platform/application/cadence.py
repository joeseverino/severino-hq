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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardRefreshRequest,
    ManagedResource,
    OperationRequest,
    ProviderConnection,
    ProviderInventory,
    ReadRequest,
)
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.provider_adapters.github import KIND as GITHUB_KIND
lazy from hq.domains.control_plane.providers import PROVIDERS, enabled_controller_actions, registry_label

from .command_fields import Describe, Names
from .derivations import asked_ahead, holds_until
from .integration_specs import requested
from .moments import duration
from .security import Principal
from .ui import ended
lazy from .action_links import READ_NOW_CAPABILITY
lazy from .adoption import manages_through, observes_only
lazy from .capabilities import capability_registry
lazy from .credential_sight import fed_kinds
lazy from .facts import stored_snapshots


@dataclass(frozen=True, slots=True)
class ControllerSweepCommand:
    """A wake-up request; with a subject, that subject is read on the next pull.

    ``connection_ref`` forces every kind its credential reads, ``kind`` one
    kind, ``every_connection`` the whole sweep. At most one of the three.
    """

    connection_ref: Annotated[
        str, Names("connection.standing"), Describe("The connection to read now, every type its credential reads.")
    ] = ""
    kind: Annotated[str, Names("readings"), Describe("The one type of reading to read now, from every connection.")] = (
        ""
    )
    every_connection: Annotated[bool, Describe("Read every connection now.")] = False


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


@dataclass(frozen=True, slots=True)
class ControllerStanding:
    """When the controller last reached HQ, and whether that is too long ago."""

    seen_at: datetime | None
    silent: bool

    @property
    def known(self) -> bool:
        return self.seen_at is not None


def controller_standing(now: datetime | None = None) -> ControllerStanding:
    """The controller's last arrival: its heartbeat, else the newest reading,
    since only a controller writes one (a database copied from another host
    has readings and no heartbeat here). Never seen is unknown, not silent: an
    installation with no controller has nothing to be late."""

    marker = _path("SEVERINO_CONTROLLER_HEARTBEAT", "controller-heartbeat")
    seen_at: datetime | None
    try:
        seen_at = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.get_current_timezone())
    except OSError:
        seen_at = newest_reading()
    if seen_at is None:
        return ControllerStanding(seen_at=None, silent=False)
    return ControllerStanding(seen_at=seen_at, silent=(now or timezone.now()) - seen_at > CONTROLLER_SILENT_AFTER)


def newest_reading() -> datetime | None:
    """When the newest stored reading was taken, whichever controller took it."""

    times = [row.observed_at for rows in stored_snapshots().values() for row in rows if row.observed_at]
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
    """SSH connections this controller should report without probing again.

    Only ones whose last probe succeeded within ``ssh_probe_interval``, so a
    recovery shows as soon as it happens. ``failing`` carries the failing ones
    too, for a sweep run only because somebody asked for a reading: a host
    refusing a login counts every further attempt, and opening pages must not
    become a login every few minutes against it.
    """

    fresh_since = timezone.now() - ssh_probe_interval()
    probes = ProviderConnection.objects.filter(
        controller_id=controller_id,
        provider="ssh",
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
    return declared is not None and not declared.unobserved_reason


def every_sweep(kind: str) -> bool:
    """Whether every sweep reads ``kind``: swept, and on no slower clock of its
    own. A kind on its own clock is carried by the sweeps between its reads, so
    its age says nothing about whether a sweep is due or has happened."""

    reading = OBSERVATIONS.get(kind)
    return swept(kind) and (reading is None or reading.every is None)


def sweep_due(controller_id: str = "") -> dict[str, object]:
    """Whether the controller should sweep now, and why.

    The oldest sweep decides, unless a pending ``ReadRequest`` makes it due
    whatever the cadence says. The reason rides along because a controller
    that stopped sweeping and one told not to look are the same from outside,
    and only one of them is a fault.
    """

    forced = forced_reads()
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
    }
    if oldest is None:
        return {**verdict, "due": True, "reason": "Nothing has been read yet."}
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
        (max(observed_at, updated_at) for kind, observed_at, updated_at in rows if every_sweep(kind)), default=None
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


# ----- Refresh ----------------------------------------------------------------


def read_request_lifetime() -> timedelta:
    """How long an unanswered read request keeps forcing sweeps."""

    return timedelta(seconds=_seconds("SEVERINO_READ_REQUEST_SECONDS"))


@dataclass(frozen=True, slots=True)
class ForcedRead:
    """One pending read request and the kinds it forces; ``None`` is every kind."""

    connection_ref: str
    kind: str
    requested_at: datetime
    kinds: tuple[str, ...] | None

    @property
    def subject(self) -> str:
        return self.connection_ref or self.kind or "every connection"

    def as_dict(self) -> dict[str, object]:
        return {
            "connection_ref": self.connection_ref,
            "kind": self.kind,
            "requested_at": self.requested_at.isoformat(),
            "kinds": list(self.kinds) if self.kinds is not None else None,
        }


def connection_providers(connection_ref: str) -> tuple[str, ...]:
    """The providers a connection ref is reported as, by any controller."""

    return tuple(
        sorted(
            set(
                ProviderConnection.objects.filter(connection_ref=connection_ref)
                .exclude(provider="")
                .values_list("provider", flat=True)
            )
        )
    )


def forced_kinds(connection_ref: str, kind: str) -> tuple[str, ...] | None:
    """The kinds a read of one connection or one kind forces; None is every kind."""

    if kind:
        return (kind,)
    if connection_ref:
        return tuple(
            dict.fromkeys(found for provider in connection_providers(connection_ref) for found in fed_kinds(provider))
        )
    return None


def _answered(read: ForcedRead, stored: dict[str, datetime], probed: dict[str, datetime]) -> bool:
    """Whether everything ``read`` forces was stored after it was asked for."""

    asked = read.requested_at
    if read.connection_ref and probed.get(read.connection_ref, asked) <= asked:
        return False
    if read.kinds is None:
        # Every kind: answered once all a sweep reads was stored after it.
        swept_at = [at for kind, at in stored.items() if every_sweep(kind)]
        return bool(swept_at) and min(swept_at) > asked
    return all(stored.get(kind, asked) > asked for kind in read.kinds)


def forced_reads() -> tuple[ForcedRead, ...]:
    """Read requests not yet answered and not yet expired, oldest first."""

    since = timezone.now() - read_request_lifetime()
    requests = tuple(ReadRequest.objects.filter(requested_at__gte=since).order_by("requested_at"))
    if not requests:
        return ()
    stored = dict(ProviderInventory.objects.values_list("kind", "updated_at"))
    probed: dict[str, datetime] = {}
    for ref, observed_at in ProviderConnection.objects.values_list("connection_ref", "observed_at"):
        probed[ref] = max(observed_at, probed.get(ref, observed_at))
    reads = (
        ForcedRead(
            request.connection_ref,
            request.kind,
            request.requested_at,
            forced_kinds(request.connection_ref, request.kind),
        )
        for request in requests
    )
    return tuple(read for read in reads if not _answered(read, stored, probed))


def settle_read_requests() -> int:
    """Forget read requests that were answered or expired; how many went."""

    pending = {(read.connection_ref, read.kind) for read in forced_reads()}
    settled = [
        request for request in ReadRequest.objects.all() if (request.connection_ref, request.kind) not in pending
    ]
    for request in settled:
        request.audit_gone = f"Read of {request.connection_ref or request.kind or 'every connection'} answered"
        request.delete()
    return len(settled)


def _read_subject(command: ControllerSweepCommand) -> tuple[str, str] | None:
    """``(connection_ref, kind)`` the command forces, or None for a bare wake-up.

    Fails closed: a ref no controller reported, or a kind HQ does not store.
    """

    ref = command.connection_ref.strip()
    kind = command.kind.strip()
    named = [field for field, value in (("connection_ref", ref), ("kind", kind)) if value]
    if command.every_connection:
        named.append("every_connection")
    if len(named) > 1:
        raise ValidationError(dict.fromkeys(named, "Name one of connection_ref, kind or every_connection."))
    if ref and not connection_providers(ref):
        raise ValidationError({"connection_ref": ValidationError("No such connection.", code="invalid_choice")})
    if kind and kind not in OBSERVATIONS and kind not in PROVIDERS:
        raise ValidationError({"kind": ValidationError("No such type.", code="invalid_choice")})
    return (ref, kind) if named else None


def pass_due() -> str:
    """Why the controller should run a pass now, or "" when a pass would find nothing.

    The doorbell (`hq.platform.core.doorbell`) asks this once a minute, and
    again as each capability's write commits: a capability's change starts a
    pass at once, any other writer's within the minute. An
    unanswered dashboard refresh stops counting after `read_request_lifetime()`.
    """

    now = timezone.now()
    if OperationRequest.objects.filter(state=OperationRequest.State.QUEUED).exists():
        return "Work is queued."
    if OperationRequest.objects.filter(state=OperationRequest.State.CLAIMED, lease_expires_at__lte=now).exists():
        return "Work a controller took did not finish in time."
    if DashboardRefreshRequest.objects.filter(
        completed_at__isnull=True, requested_at__gte=now - read_request_lifetime()
    ).exists():
        return "A dashboard reading was asked for."
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
    """Ring if a pass is due; whether it rang. Asked by the doorbell, once a
    minute and as a capability's write commits, so `pass_due` alone decides."""

    return bool(pass_due()) and ring_doorbell()


def ring_doorbell() -> bool:
    """Tell the host something is queued, without telling it anything else.

    The doorbell is a file a unit on the host watches to start the controller,
    which pulls the work its usual way: forged, deleted or replayed, the worst
    it causes is a run that finds nothing.
    """

    return _ring(_controller_doorbell())


def ring_glance_doorbell() -> bool:
    """Ask the host for the dashboard readings HQ is waiting on, and nothing else.

    Its own file beside the controller's doorbell starts a pass of its own that
    reads only those panels, so a refresh never waits behind a sweep under way.
    Forged or replayed, it causes a pass that finds nothing asked for.
    """

    return _ring(_controller_doorbell().with_name("glance-doorbell"))


def _controller_doorbell() -> Path:
    return _path("SEVERINO_CONTROLLER_DOORBELL", "controller-doorbell")


def _ring(doorbell: Path) -> bool:
    """Best effort, and rung only after what it announces is stored, so asking
    never depends on the host filesystem."""

    try:
        _touch(doorbell)
    except OSError:
        return False
    return True


def request_delivery_read() -> bool:
    """Ask for a read of ``github.delivery`` as HQ boots on a new image.

    A deploy is when production changes which commit of each extension it
    runs, so delivery has something to report: each commit's check run, and a
    comment on its merged pull request. Asked by the boot, not as a person's
    activity, so the idle cadence is untouched.
    """

    # Only where delivery is read at all: a request nothing answers would force
    # sweeps until it expired.
    if not (
        ProviderInventory.objects.filter(kind=GITHUB_KIND).exists()
        or ManagedResource.objects.filter(kind=GITHUB_KIND).exists()
    ):
        return False
    return request_reading(GITHUB_KIND)


def request_reading(kind: str) -> bool:
    """Ask for one kind to be read on the controller's next run, and start one.

    For HQ and the host to ask on what they have just learned, not for a
    person: nobody's activity is noted, so the idle cadence is untouched and
    the sweep this causes reads the one kind. The request is stored whether or
    not the doorbell could be rung; the controller's timer finds it within its
    floor. The answer is whether it was rung.
    """

    ReadRequest.objects.update_or_create(connection_ref="", kind=kind, defaults={"requested_at": timezone.now()})
    return ring_doorbell()


def request_controller_sweep(
    command: ControllerSweepCommand,
    *,
    principal: Principal,
) -> dict[str, object]:
    """Wake the pull-based controller; with a subject, have it read that now."""

    del principal
    subject = _read_subject(command)
    # An operator asking for fresh state makes HQ active before policy is read,
    # so the active cadence (not the twelve-hour idle economy) decides the sweep.
    note_activity()
    if subject is not None:
        ref, kind = subject
        ReadRequest.objects.update_or_create(connection_ref=ref, kind=kind, defaults={"requested_at": timezone.now()})
    verdict = sweep_due()
    if not ring_doorbell():
        raise ValueError("HQ could not reach the controller to start it.")
    return {
        "ok": True,
        "requested": True,
        "due": verdict["due"],
        "reason": verdict["reason"],
        "read_now": _read_label(subject),
        "message": _sweep_message(subject, due=bool(verdict["due"])),
    }


@transaction.atomic
def request_reads(kinds: Iterable[str]) -> tuple[str, ...]:
    """Ask for several kinds to be read now, as one request: the kinds asked for.

    Each is held to the same test a single read is, so nothing is asked for
    that HQ does not store a kind for. Activity is noted once and the doorbell
    rung once, after the requests are stored: rung before, the controller could
    arrive to find nothing asked.
    """

    wanted = [
        subject[1]
        for kind in dict.fromkeys(kinds)
        if (subject := _read_subject(ControllerSweepCommand(kind=kind))) is not None
    ]
    if not wanted:
        return ()
    note_activity()
    now = timezone.now()
    for kind in wanted:
        ReadRequest.objects.update_or_create(connection_ref="", kind=kind, defaults={"requested_at": now})
    transaction.on_commit(ring_doorbell)
    return tuple(wanted)


def _read_label(subject: tuple[str, str] | None) -> str:
    if subject is None:
        return ""
    ref, kind = subject
    return ref or kind or "every connection"


def _sweep_message(subject: tuple[str, str] | None, *, due: bool) -> str:
    """What asking says, in the refresh command's own words."""

    refresh = capability_registry()[READ_NOW_CAPABILITY]
    ref, kind = subject or ("", "")
    if ref or kind:
        return refresh.requested(ref or registry_label(kind))
    asked = requested(refresh.words("all"))
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
