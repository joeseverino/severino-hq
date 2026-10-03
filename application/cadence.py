"""When the controller should sweep, and how it hears that there is work.

Applying a queued operation and sweeping what the providers hold run on
different clocks. The first should happen the moment it is asked for; the second
describes records that change monthly, and asking more often buys nothing and
costs a provider call each time. Driven by one timer, only one of them can be
right.

Both halves keep the trust direction that makes HQ safe. The web process holds
no provider credential, so a compromise of it cannot reach a provider or open a
shell anywhere. Trust runs one way: the privileged controller reaches into HQ
and pulls work, and HQ never reaches out.

Cadence is policy, and policy belongs where the observations are. HQ records
when each provider was last swept, so HQ answers "is one due?" and the controller
executes: the same split as claim, schedule and report.

The doorbell is a file HQ touches when it queues something. It carries no
authority, no credentials and no data: it cannot say what to do, only that
something changed. A unit on the host watches it and starts the controller,
which pulls the work through its usual path. Forged, deleted or
replayed, the worst it can cause is a controller run that finds nothing to do.
"""


from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import os
import tempfile
import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from control_plane.models import ProviderConnection, ProviderInventory, ReadRequest

from .security import Capability, Principal


@dataclass(frozen=True)
class ControllerSweepCommand:
    """A wake-up request; with a subject, that subject is read on the next pull.

    ``connection_ref`` forces every kind its credential reads, ``kind`` one
    kind, ``every_connection`` the whole sweep. At most one of the three.
    """

    connection_ref: str = ""
    kind: str = ""
    every_connection: bool = False


def _seconds(name: str, fallback: int) -> int:
    try:
        return max(0, int(getattr(settings, name, fallback)))
    except (TypeError, ValueError):
        return fallback


def _path(name: str, filename: str) -> Path:
    configured = str(getattr(settings, name, "") or "").strip()
    if configured:
        return Path(configured)
    # Beside the database, which is the volume both sides share. Anywhere else
    # is visible in the container and invisible from the host, which is the one
    # place the watcher runs.
    return Path(settings.DATABASES["default"]["NAME"]).parent / filename


def note_activity() -> None:
    """Record that somebody is using HQ, cheaply enough to do on every request.

    A file's mtime rather than a column: this runs on every page load, and the
    fact is too coarse to be worth a database write. Rewritten at most once per
    interval, so the common case is a stat and nothing else.

    Best effort. Nothing an operator asked for may fail because a hint about
    scheduling could not be written.
    """

    marker = _path("SEVERINO_ACTIVITY_MARKER", "hq-activity")
    throttle = _seconds("SEVERINO_ACTIVITY_THROTTLE_SECONDS", 60)
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < throttle:
            return
        _touch(marker)
    except OSError:
        return


def recently_used(now: float | None = None) -> bool:
    """Whether HQ has been used inside the active window."""

    marker = _path("SEVERINO_ACTIVITY_MARKER", "hq-activity")
    window = _seconds("SEVERINO_ACTIVE_WINDOW_SECONDS", 900)
    try:
        age = (time.time() if now is None else now) - marker.stat().st_mtime
    except OSError:
        return False
    return age <= window


def note_controller() -> None:
    """Record that the controller reached HQ, on the call every real run makes.

    A file's mtime, like the activity marker and for the same reason: it is
    written about once a minute and read by one page. What it is for is the
    case no record of a sweep can show, a controller that stopped arriving at
    all: every reading then stays as it was, which looks like a quiet
    estate until somebody notices nothing has moved.

    Best effort. The controller's work may not fail because a note about it
    could not be written.
    """

    try:
        _touch(_path("SEVERINO_CONTROLLER_HEARTBEAT", "controller-heartbeat"))
    except OSError:
        return


# A run starts within a minute of the last ending and a sweep takes a few, so
# a healthy controller is never this long between arrivals.
CONTROLLER_SILENT_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class ControllerStanding:
    """When the controller last reached HQ, and whether that is too long ago."""

    seen_at: datetime | None
    silent: bool

    @property
    def known(self) -> bool:
        return self.seen_at is not None


def controller_standing(now: datetime | None = None) -> ControllerStanding:
    """The controller's last arrival. Never seen is unknown, not silent: an
    installation with no controller has nothing to be late."""

    marker = _path("SEVERINO_CONTROLLER_HEARTBEAT", "controller-heartbeat")
    try:
        seen_at = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.get_current_timezone())
    except OSError:
        return ControllerStanding(seen_at=None, silent=False)
    return ControllerStanding(
        seen_at=seen_at, silent=(now or timezone.now()) - seen_at > CONTROLLER_SILENT_AFTER
    )


def sweep_interval() -> timedelta:
    """How stale a sweep may be before another is worth the calls.

    Adaptive, because the answer depends on whether anybody is looking. In use,
    a view of the estate a few minutes old is the point of having one, and the
    interval stays longer than a sweep takes so the controller is idle, and
    answers its doorbell, between them. Idle, twelve hours of staleness costs
    nothing and saves the calls.
    """

    active = _seconds("SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS", 300)
    idle = _seconds("SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS", 12 * 60 * 60)
    return timedelta(seconds=active if recently_used() else idle)


def slowest_sweep_interval() -> timedelta:
    """The longest gap between sweeps the policy permits, whoever is watching.

    The ceiling rather than the current value, for anything that needs a stable
    number: a staleness threshold derived from the live interval moves with
    the thing it is measuring.
    """

    return timedelta(seconds=_seconds("SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS", 12 * 60 * 60))


def ssh_probe_interval() -> timedelta:
    """How long a working SSH connection's last answer stays good enough.

    A probe of an SSH connection is a real login: a certificate minted or a key
    offered, a session opened, a command run. That is fine twice a day and not
    fine every minute, which is what the active sweep cadence would make it,
    and a shared host counts those logins, and may act on them. Everything else
    a sweep reads is cheap enough to keep on the sweep's own clock.
    """

    return timedelta(seconds=_seconds("SEVERINO_SSH_PROBE_INTERVAL_SECONDS", 60 * 60))


def carried_connections(controller_id: str, *, failing: bool = False) -> list[str]:
    """SSH connections this controller should report without probing again.

    On the sweep's own clock, only ones whose last probe succeeded and is
    younger than the interval: a failing connection is asked again, so a
    recovery shows up as soon as it happens rather than an hour later.

    ``failing`` carries the failing ones too. That is for a sweep that runs
    only because somebody asked for a reading: it was not asked to look at
    SSH, and a host that is refusing a login counts every further attempt.
    Opening pages must not turn into a login every couple of minutes against
    a host that has already said no.
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


def sweep_due(controller_id: str = "") -> dict[str, object]:
    """Whether the controller should sweep now, and why.

    The oldest sweep decides, unless an operator asked for a read: a pending
    ``ReadRequest`` makes the sweep due whatever the cadence says. The reason
    rides along because a controller that stopped sweeping and one that was
    told not to look identical from outside, and only one of them is a fault.

    `carry` names the connections the sweep should report as they last were
    rather than probe; see `carried_connections`. A forced connection is never
    carried. `only_kinds`, when not empty, is every kind the sweep reads: set
    when the cadence is not due and every pending request names its kinds.
    """

    forced = forced_reads()
    forced_refs = {read.connection_ref for read in forced if read.connection_ref}
    carry = (
        [ref for ref in carried_connections(controller_id) if ref not in forced_refs]
        if controller_id
        else []
    )
    interval = sweep_interval()
    oldest = (
        ProviderInventory.objects.order_by("observed_at")
        .values_list("observed_at", flat=True)
        .first()
    )
    verdict: dict[str, object] = {
        "ok": True,
        "interval_seconds": int(interval.total_seconds()),
        "carry": carry,
        "forced": [read.as_dict() for read in forced],
        "only_kinds": [],
    }
    if oldest is None:
        return {**verdict, "due": True, "reason": "Nothing has been swept yet."}
    age = timezone.now() - oldest
    due = age >= interval
    reason = (
        f"Oldest sweep is {int(age.total_seconds())}s old; "
        f"{'due' if due else 'not due'} at {int(interval.total_seconds())}s."
    )
    if not due and forced:
        reason += f" Read now asked for {', '.join(read.subject for read in forced)}."
        if controller_id:
            # Due only because somebody asked: SSH is left as it was last
            # found, working or not, unless it is what they asked for.
            verdict["carry"] = [
                ref
                for ref in carried_connections(controller_id, failing=True)
                if ref not in forced_refs
            ]
        if all(read.kinds is not None for read in forced):
            verdict["only_kinds"] = sorted(
                {kind for read in forced for kind in read.kinds or ()}
            )
    return {
        **verdict,
        "due": due or bool(forced),
        "reason": reason,
        "age_seconds": int(age.total_seconds()),
    }


# ----- Read now ---------------------------------------------------------------


def read_request_lifetime() -> timedelta:
    """How long an unanswered read request keeps forcing sweeps."""

    return timedelta(seconds=_seconds("SEVERINO_READ_REQUEST_SECONDS", 15 * 60))


@dataclass(frozen=True)
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


def _forced_kinds(connection_ref: str, kind: str) -> tuple[str, ...] | None:
    from .credential_sight import fed_kinds

    if kind:
        return (kind,)
    if connection_ref:
        return tuple(
            dict.fromkeys(
                found
                for provider in connection_providers(connection_ref)
                for found in fed_kinds(provider)
            )
        )
    return None


def _answered(
    read: ForcedRead, stored: dict[str, datetime], probed: dict[str, datetime]
) -> bool:
    """Whether everything ``read`` forces was stored after it was asked for."""

    asked = read.requested_at
    if read.connection_ref and probed.get(read.connection_ref, asked) <= asked:
        return False
    if read.kinds is None:
        # Every kind: answered once something was stored and all of it is newer.
        return bool(stored) and min(stored.values()) > asked
    return all(stored.get(kind, asked) > asked for kind in read.kinds)


def forced_reads() -> tuple[ForcedRead, ...]:
    """Read requests not yet answered and not yet expired, oldest first."""

    since = timezone.now() - read_request_lifetime()
    requests = tuple(
        ReadRequest.objects.filter(requested_at__gte=since).order_by("requested_at")
    )
    if not requests:
        return ()
    stored = dict(ProviderInventory.objects.values_list("kind", "updated_at"))
    probed: dict[str, datetime] = {}
    for ref, observed_at in ProviderConnection.objects.values_list(
        "connection_ref", "observed_at"
    ):
        probed[ref] = max(observed_at, probed.get(ref, observed_at))
    reads = (
        ForcedRead(
            request.connection_ref,
            request.kind,
            request.requested_at,
            _forced_kinds(request.connection_ref, request.kind),
        )
        for request in requests
    )
    return tuple(read for read in reads if not _answered(read, stored, probed))


def settle_read_requests() -> int:
    """Forget read requests that were answered or expired; how many went."""

    pending = {(read.connection_ref, read.kind) for read in forced_reads()}
    settled = [
        request
        for request in ReadRequest.objects.all()
        if (request.connection_ref, request.kind) not in pending
    ]
    for request in settled:
        request.audit_gone = f"Read of {request.connection_ref or request.kind or 'every connection'} answered"
        request.delete()
    return len(settled)


def _read_subject(command: ControllerSweepCommand) -> tuple[str, str] | None:
    """``(connection_ref, kind)`` the command forces, or None for a bare wake-up.

    Fails closed: a ref no controller reported, or a kind HQ does not store.
    """

    from control_plane.observations import OBSERVATIONS
    from control_plane.providers import PROVIDERS

    ref = command.connection_ref.strip()
    kind = command.kind.strip()
    named = [field for field, value in (("connection_ref", ref), ("kind", kind)) if value]
    if command.every_connection:
        named.append("every_connection")
    if len(named) > 1:
        raise ValidationError(
            {field: "Name one of connection_ref, kind or every_connection." for field in named}
        )
    if ref and not connection_providers(ref):
        raise ValidationError(
            {"connection_ref": ValidationError("No such connection.", code="invalid_choice")}
        )
    if kind and kind not in OBSERVATIONS and kind not in PROVIDERS:
        raise ValidationError({"kind": ValidationError("No such kind.", code="invalid_choice")})
    return (ref, kind) if named else None


def ring_doorbell() -> bool:
    """Tell the host something is queued, without telling it anything else.

    Best effort, and rung only after the operation is stored. A doorbell able to
    fail the write it announces would make queueing depend on the host
    filesystem, which is the opposite of what it is for.
    """

    try:
        _touch(_path("SEVERINO_CONTROLLER_DOORBELL", "controller-doorbell"))
    except OSError:
        return False
    return True


def ring_registry_doorbell() -> bool:
    """Tell the host a sweep found an image or digest HQ has not read, so it
    starts ``refresh_public_registry`` now rather than at the daily floor.
    Carries nothing, like the controller's."""

    try:
        _touch(_path("SEVERINO_REGISTRY_DOORBELL", "registry-doorbell"))
    except OSError:
        return False
    return True


def request_delivery_read() -> bool:
    """Ask for a read of ``github.delivery`` as HQ boots on a new image.

    A deploy is the moment production changes which commit of each extension
    it runs, so it is the moment delivery has something to report: the check
    run on each commit, and one comment on its merged pull request. Asked for
    by the boot rather than found by a schedule, and not as a person's
    activity, so the idle cadence is untouched. Only where delivery is read at
    all: a request nothing answers would force sweeps until it expired.
    """

    from control_plane.models import ManagedResource
    from control_plane.provider_adapters.github import KIND as kind

    if not (
        ProviderInventory.objects.filter(kind=kind).exists()
        or ManagedResource.objects.filter(kind=kind).exists()
    ):
        return False
    ReadRequest.objects.update_or_create(
        connection_ref="", kind=kind, defaults={"requested_at": timezone.now()}
    )
    return ring_doorbell()


def request_controller_sweep(
    command: ControllerSweepCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, object]:
    """Wake the pull-based controller; with a subject, have it read that now."""

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    subject = _read_subject(command)
    # An operator asking for fresh state makes HQ active before policy is read,
    # so the active cadence (not the twelve-hour idle economy) decides the sweep.
    note_activity()
    if subject is not None:
        ref, kind = subject
        ReadRequest.objects.update_or_create(
            connection_ref=ref, kind=kind, defaults={"requested_at": timezone.now()}
        )
    verdict = sweep_due()
    if not ring_doorbell():
        raise ValueError("The controller doorbell could not be reached.")
    return {
        "ok": True,
        "requested": True,
        "due": verdict["due"],
        "reason": verdict["reason"],
        "read_now": _read_label(subject),
        "message": _sweep_message(subject, bool(verdict["due"])),
    }


@transaction.atomic
def request_reads(kinds: Iterable[str], *, principal: Principal) -> tuple[str, ...]:
    """Ask for several kinds to be read now, as one request: the kinds asked for.

    Each is held to the same test a single read is, so nothing is asked for
    that HQ does not store a kind for. Activity is noted once and the doorbell
    rung once, after the requests are stored: rung before, the controller could
    arrive to find nothing asked.
    """

    principal.require(Capability.MANAGE_INFRASTRUCTURE)
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
        ReadRequest.objects.update_or_create(
            connection_ref="", kind=kind, defaults={"requested_at": now}
        )
    transaction.on_commit(ring_doorbell)
    return tuple(wanted)


def _read_label(subject: tuple[str, str] | None) -> str:
    if subject is None:
        return ""
    ref, kind = subject
    return ref or kind or "every connection"


def _sweep_message(subject: tuple[str, str] | None, due: bool) -> str:
    if subject is not None:
        return (
            f"Asked the controller to read {_read_label(subject)} now; "
            "this page updates when it reports."
        )
    if due:
        return "The controller was notified and will pull work now."
    return "The controller was notified; the current observation is already fresh."


def _touch(path: Path) -> None:
    """Replace a marker, so a watcher sees an event it cannot coalesce away.

    `Path.touch` on an existing file is a bare utime, which inotify may fold
    into nothing. A replacement is a create, and `os.replace` is atomic, so a
    reader never catches the file absent.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".marker-")
    os.close(handle)
    os.replace(temporary, path)
