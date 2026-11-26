"""The reads somebody asked for and HQ has not yet stored: which are pending,
what each forces, and which a sweep answered.

``cadence.ask`` stores a request; the sweep's verdict (``cadence.sweep_due``)
reads them here, and a sweep's report settles them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory, ReadRequest
lazy from hq.platform.core.attribution import continuing

from .derivations import reached
lazy from .cadence import every_sweep
lazy from .credential_sight import fed_kinds
lazy from .projection import iso

# How long after a kind was stored an event's ask for it waits, so a burst of
# events is a read at once and one more after it rather than a pass for each:
# a reading two minutes behind is still fresh, and every pass spends the
# providers' request budgets. No longer than a request lives, so the one that
# waits is still pending when the gap ends. A person's ask never waits.
EVENT_READ_GAP = timedelta(minutes=2)


def read_request_lifetime() -> timedelta:
    """How long an unanswered read request keeps forcing sweeps."""

    return timedelta(seconds=max(0, int(settings.SEVERINO_READ_REQUEST_SECONDS)))


@dataclass(frozen=True, slots=True)
class ForcedRead:
    """One pending read request and the kinds it forces; ``None`` is every kind."""

    connection_ref: str
    kind: str
    requested_at: datetime
    kinds: tuple[str, ...] | None
    # The creation context of its latest ask, which its answer continues.
    traceparent: str = ""
    # When an event's ask for a kind read lately may force a pass, or None
    # for one that may at once.
    waits_until: datetime | None = None

    @property
    def subject(self) -> str:
        return self.connection_ref or self.kind or "every connection"

    def as_dict(self) -> dict[str, object]:
        return {
            "connection_ref": self.connection_ref,
            "kind": self.kind,
            "requested_at": iso(self.requested_at),
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


def asked_at(kind: str) -> datetime | None:
    """When a read of this kind was asked for and has not been tried yet."""

    return next((read.requested_at for read in forced_reads() if read.kind == kind), None)


def forced_reads(asked: Iterable[ReadRequest] | None = None) -> tuple[ForcedRead, ...]:
    """Read requests not yet answered and not yet expired, oldest first: of
    ``asked`` where a caller holds every request already, else of all."""

    since = timezone.now() - read_request_lifetime()
    if asked is None:
        asked = ReadRequest.objects.filter(requested_at__gte=since)
    requests = sorted(
        (request for request in asked if request.requested_at >= since), key=lambda request: request.requested_at
    )
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
            (kinds := forced_kinds(request.connection_ref, request.kind)),
            request.traceparent,
            _spaced(request, kinds, stored),
        )
        for request in requests
    )
    return tuple(read for read in reads if not _answered(read, stored, probed))


def _spaced(request: ReadRequest, kinds: tuple[str, ...] | None, stored: dict[str, datetime]) -> datetime | None:
    """When an event's ask may force a pass: ``EVENT_READ_GAP`` after the
    kinds it forces were stored, or None once one of them never was."""

    if not request.spaced or not kinds or any(kind not in stored for kind in kinds):
        return None
    return min(stored[kind] for kind in kinds) + EVENT_READ_GAP


def due_reads(forced: tuple[ForcedRead, ...]) -> tuple[ForcedRead, ...]:
    """Of ``forced``, the reads that force a pass now: an event's waits
    out its gap, still pending, so nothing it asked for is lost."""

    return tuple(read for read in forced if read.waits_until is None or reached(read.waits_until))


def askers(kinds: Iterable[str]) -> dict[str, str]:
    """The creation context of the newest pending read that forces each of
    ``kinds``, by kind, read before a sweep stores them, so what the sweep
    writes of a kind joins the chain that asked for it."""

    found: dict[str, str] = {}
    for read in reversed(forced_reads()):
        for kind in kinds if read.kinds is None else read.kinds:
            found.setdefault(kind, read.traceparent)
    return found


def settle_read_requests() -> int:
    """Forget read requests that were answered or expired; how many went."""

    every = list(ReadRequest.objects.all())
    pending = {(read.connection_ref, read.kind) for read in forced_reads(every)}
    settled = [request for request in every if (request.connection_ref, request.kind) not in pending]
    for request in settled:
        request.audit_gone = f"Read of {request.connection_ref or request.kind or 'every connection'} answered"
        with continuing(request.traceparent):
            request.delete()
    return len(settled)
