"""The reads somebody asked for and HQ has not yet stored: which are pending,
what each forces, and which a sweep answered.

``cadence.ask`` stores a request; the sweep's verdict (``cadence.sweep_due``)
reads them here, and a controller's pass marks each it takes (``take``). Only a pass
that took a request answers it, by storing what it forces after taking it: a
pass that read before an ask and stored after it never answers that ask. A
burst of asks coalesces on the pass itself, since the controller runs one
pass at a time and every ask during one is taken by the next.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory, ReadRequest
from hq.domains.control_plane.observations.contract import read_together
lazy from hq.platform.core.attribution import continuing

lazy from .cadence import every_sweep
lazy from .credential_sight import fed_kinds
lazy from .projection import iso


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
    # What of the kind it asks for, "" for all of it; and when a pass took it.
    subject: str = ""
    taken_at: datetime | None = None

    @property
    def label(self) -> str:
        return " ".join(filter(None, (self.connection_ref or self.kind or "every connection", self.subject)))

    def as_dict(self) -> dict[str, object]:
        return {
            "connection_ref": self.connection_ref,
            "kind": self.kind,
            "subject": self.subject,
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
    """The kinds a read of one connection or one kind forces, with every kind
    read in the same pass as one (``read_together``); None is every kind."""

    if kind:
        return tuple(sorted(read_together((kind,))))
    if connection_ref:
        return tuple(
            dict.fromkeys(found for provider in connection_providers(connection_ref) for found in fed_kinds(provider))
        )
    return None


def _answered(read: ForcedRead, stored: dict[str, datetime], probed: dict[str, datetime]) -> bool:
    """Whether a pass took ``read`` and stored everything it forces since."""

    asked = read.taken_at
    if asked is None:
        return False
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
            forced_kinds(request.connection_ref, request.kind),
            request.traceparent,
            request.subject,
            request.taken_at,
        )
        for request in requests
    )
    return tuple(read for read in reads if not _answered(read, stored, probed))


def take(forced: tuple[ForcedRead, ...]) -> tuple[ForcedRead, ...]:
    """Mark ``forced`` taken now by the pass handed them, and hand them back. Each
    is matched on the ask it was read as, so an ask made since stays for the next pass."""

    at = timezone.now()
    for read in forced:
        ReadRequest.objects.filter(
            connection_ref=read.connection_ref, kind=read.kind, subject=read.subject, requested_at=read.requested_at
        ).update(taken_at=at)
    return forced


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
    pending = {(read.connection_ref, read.kind, read.subject) for read in forced_reads(every)}
    settled = [request for request in every if (request.connection_ref, request.kind, request.subject) not in pending]
    for request in settled:
        named = " ".join(filter(None, (request.connection_ref or request.kind or "every connection", request.subject)))
        request.audit_gone = f"Read of {named} answered"
        with continuing(request.traceparent):
            request.delete()
    return len(settled)
