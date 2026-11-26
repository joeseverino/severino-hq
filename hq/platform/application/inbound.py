"""The one door by which HQ is told something happened outside it.

Every source reports through ``apply_events``: the listener on the Worker's
socket over the bridge's ``events`` action, and HQ's own host jobs. Each
event is stored once per delivery and makes due the readings its provider's
``INBOUND`` declaration names, through ``cadence.ask``, so a push is read in
seconds rather than at the next sweep.

Security: an event carries no truth, only a name. HQ never reads its body or
acts on what it claims; it asks for readings it would take anyway and reads
them with its own credentials. A forged event costs one read, coalesced with
any already asked; a replayed delivery is the stored event and asks nothing;
an event no declaration names is recorded as ignored and asks nothing; a
reconnect asks every reading its source carries, bounded by the reconnect's
own backoff and ``ask``'s one request per kind; a provider no declaration
names, or a batch outside the contract's ``InboundBatch``, is refused whole. Only the bridge's action, which serves the
controller's account alone on a 0700 socket, and the host's jobs may import
this (``lint:imports``), and each write is audited under its door's title.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from hq.domains.control_plane.models import InboundEvent, InboundOutcome, InboundSource
lazy from hq.domains.control_plane.bridge_contract import departs
lazy from hq.domains.control_plane.provider_adapters import INBOUND

from . import readings
from .inbound_events import channel
from .moments import moment
lazy from .cadence import ask

# The contract's name for a batch, the one shape every source reports in.
BATCH = "InboundBatch"
# A channel reports far more often than an age is worded.
_NOTED_EVERY = timedelta(minutes=1)


def _carried(source: InboundSource) -> list[str]:
    """Every reading the events ``source`` carries make due: what a gap in its
    channel may have missed. The Worker carries each provider it routes."""

    routed = source == InboundSource.WORKER
    return [
        kind
        for spec in INBOUND.values()
        if bool(spec.path) == routed
        for kinds in spec.events.values()
        for kind in kinds
    ]


@transaction.atomic
def apply_events(batch: Mapping[str, Any]) -> dict[str, Any]:
    """Store each new event in ``batch`` and ask once for every reading they
    make due; how many were applied, repeated and ignored, and the kinds asked.

    ``batch`` is the contract's ``InboundBatch``. One with no events says its
    source is listening and nothing happened. One that says its source
    reconnected asks every reading that source's events make due, since a
    gap is repaired by reading again, never by replaying what was missed.
    """

    if departed := departs(BATCH, batch):
        raise ValueError(departed)
    source = InboundSource(batch["source"])
    readings.note(channel(source), every=_NOTED_EVERY)
    counts = dict.fromkeys(("applied", "repeated", "ignored"), 0)
    asked: dict[str, None] = {}
    if batch.get("reconnected"):
        asked.update(dict.fromkeys(_carried(source)))
    for arrived in batch["events"]:
        provider, delivery = arrived["provider"], arrived["delivery_id"]
        if InboundEvent.objects.filter(provider=provider, delivery_id=delivery).exists():
            counts["repeated"] += 1
            continue
        kinds = INBOUND[provider].reads(arrived["event"])
        outcome = InboundOutcome.APPLIED if kinds else InboundOutcome.IGNORED
        InboundEvent.objects.create(
            provider=provider,
            event=arrived["event"],
            subject=arrived.get("subject", ""),
            delivery_id=delivery,
            source=source,
            received_at=moment(arrived["received_at"]),
            outcome=outcome,
            reads=list(kinds),
        )
        counts[str(outcome)] += 1
        asked.update(dict.fromkeys(kinds))
    if asked:
        ask(reads=asked, spaced=True)
    return {"ok": True, **counts, "asked": list(asked)}


def prune_events(_progress: Any = None, *, subject: str = "", principal: Any = None) -> dict[str, Any]:
    """Delete the events past the routine window: each did its work as it
    arrived, and the readings it asked for hold what changed."""

    del subject, principal
    days = int(settings.SEVERINO_AUDIT_ROUTINE_DAYS)
    # Each deletion is audited as itself.
    deleted, _ = InboundEvent.objects.filter(received_at__lt=timezone.now() - timedelta(days=days)).delete()
    return {"deleted": deleted, "days": days}
