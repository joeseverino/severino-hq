"""Where requests to HQ came from, remembered per source device.

The one reading HQ takes of real traffic rather than of an API: which device or
address reached HQ, how (the tailnet leg, the proxy that forwarded it), when it
last did, and how many requests in the current window. ``note`` counts every
request in memory and writes at most once per source per
``SEVERINO_REQUEST_PATH_SECONDS`` in each process, best effort: a request never
waits on a failed write and never fails because of one. A record older than
``WINDOW`` is dropped on write and ignored on read, and at most
``MAX_SOURCES`` are kept.

A page load never writes: a safe request (GET, HEAD, OPTIONS) is only counted in
memory, with how its source arrived. The next unsafe request (a POST such as the
dashboard's glance refresh) writes every source that is due, in one write.

The record schema (``control_plane.observations.hq.ArrivalRecord``) admits
nothing else: no path, query, header value, body or user agent.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from django.conf import settings
from django.utils import timezone

from control_plane.observations.hq import ARRIVAL_KIND

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
MAX_SOURCES = 64

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


@dataclass
class _Pending:
    # When this process last wrote the source, and requests counted since.
    written: float | None = None
    count: int = 0
    arrival: dict[str, Any] | None = None


_pending: dict[str, _Pending] = {}
_lock = threading.Lock()


def _every() -> int:
    return int(getattr(settings, "SEVERINO_REQUEST_PATH_SECONDS", 0) or 0)


def note(request) -> None:
    """Count this request; on an unsafe one, write every source that is due."""

    every = _every()
    if every <= 0:
        return
    from core.network import client_ip

    address = client_ip(request)
    if not address:
        return
    safe = request.method in SAFE_METHODS
    with _lock:
        entry = _pending.setdefault(address, _Pending())
        entry.count += 1
        known = entry.arrival is not None
        due = entry.written is None or monotonic() - entry.written >= every
        _trim()
    # How it arrived is read once, and again only when it is about to be written.
    if not known or (due and not safe):
        arrival = _arrival(request, address)
        with _lock:
            entry.arrival = arrival
    if safe:
        return
    try:
        written = _due(every)
        if written:
            _write(written)
    except Exception as exc:  # noqa: BLE001 - an arrival never fails a request
        logger.warning(
            "An arrival could not be recorded: %s",
            exc.__class__.__name__,
            extra={"event": "hq.arrival.unrecorded"},
        )


def _trim() -> None:
    if len(_pending) > MAX_SOURCES * 4:
        oldest = sorted(_pending, key=lambda key: _pending[key].written or 0.0)
        for stale in oldest[:MAX_SOURCES]:
            _pending.pop(stale, None)


def _due(every: int) -> list[tuple[dict[str, Any], int]]:
    """Each counted source not written within ``every``, marked written now."""

    now = monotonic()
    due = []
    with _lock:
        for entry in _pending.values():
            if not entry.count or entry.arrival is None:
                continue
            if entry.written is not None and now - entry.written < every:
                continue
            due.append((entry.arrival, entry.count))
            entry.written, entry.count = now, 0
    return due


def forget() -> None:
    """Drop every count this process holds."""

    with _lock:
        _pending.clear()


def _write(due: list[tuple[dict[str, Any], int]]) -> None:
    """Merge each ``(arrival, count)`` into the stored reading, in one write."""

    if not due:
        return

    from control_plane.models import ProviderInventory

    from .inventory import record_inventory
    from .security import internal_principal

    now = timezone.now()
    stored = ProviderInventory.objects.filter(kind=ARRIVAL_KIND).first()
    kept = {
        _key(item): item
        for item in (stored.records if stored is not None else ())
        if isinstance(item, dict) and _current(item, now)
    }
    for fresh, count in due:
        key = _key(fresh)
        previous = kept.get(key) or {}
        restart = not _current({"last_seen": previous.get("window_start")}, now)
        kept[key] = {
            **fresh,
            "first_seen": previous.get("first_seen") or now.isoformat(),
            "last_seen": now.isoformat(),
            "window_start": now.isoformat() if restart else previous["window_start"],
            "count": count + (0 if restart else int(previous.get("count") or 0)),
        }
    newest = sorted(kept.values(), key=lambda item: item["last_seen"], reverse=True)
    record_inventory(
        {ARRIVAL_KIND: {"ok": True, "records": newest[:MAX_SOURCES]}},
        principal=internal_principal("hq.request-path"),
    )


def _arrival(request, address: str) -> dict[str, Any]:
    """What identifies the source and how it arrived; nothing else."""

    from core.network import is_trusted_proxy

    from . import tailnet
    from .request_channel import channel_for_request, forwarded_chain, socket_peer
    from .tailnet_presence import tailnet_presence

    device = tailnet.device_at(address)
    presence = tailnet_presence().get(device.name) if device is not None else None
    peer = socket_peer(request)
    forwarded = bool(forwarded_chain(request)) and is_trusted_proxy(peer)
    return {
        "address": address,
        "device": device.name if device is not None else "",
        "channel": channel_for_request(request).id,
        "carried": presence.peer_path if presence is not None else "",
        "forwarded_by": peer if forwarded else "",
    }


def _key(item: dict[str, Any]) -> str:
    return str(item.get("device") or item.get("address") or "")


def _current(item: dict[str, Any], now: datetime) -> bool:
    from .timestamps import moment

    seen = moment(str(item.get("last_seen") or ""))
    return seen is not None and now - seen <= WINDOW


# ----- Reading it back -----------------------------------------------------------

_CARRIED = {
    "direct": "directly over the tailnet",
    "relayed": "relayed over the tailnet",
    "negotiating": "over the tailnet",
}
_CHANNELS = {
    "tailnet": "over the tailnet",
    "network": "from the local network",
    "loopback": "from HQ's own machine",
}


@dataclass(frozen=True)
class Arrival:
    """The last time one source reached HQ, and how."""

    address: str
    device: str
    channel: str
    carried: str
    forwarded_by: str
    last_seen: datetime
    count: int

    @property
    def how(self) -> str:
        way = _CARRIED.get(self.carried) or _CHANNELS.get(self.channel, "from an address HQ cannot place")
        return f"{way}, through the proxy at {self.forwarded_by}" if self.forwarded_by else way

    @property
    def phrase(self) -> str:
        """"3 hours ago, directly over the tailnet"."""

        from .moments import ago

        return f"{ago(self.last_seen)}, {self.how}"


def arrivals(snapshots=()) -> dict[str, Arrival]:
    """Each current arrival by its device name, else its address."""

    from .timestamps import moment

    now = timezone.now()
    found: dict[str, Arrival] = {}
    for snapshot in snapshots:
        for item in snapshot.records or ():
            if not isinstance(item, dict) or not _current(item, now):
                continue
            found[_key(item)] = Arrival(
                address=str(item.get("address", "")),
                device=str(item.get("device", "")),
                channel=str(item.get("channel", "")),
                carried=str(item.get("carried", "")),
                forwarded_by=str(item.get("forwarded_by", "")),
                last_seen=moment(str(item["last_seen"])),
                count=int(item.get("count") or 0),
            )
    return found
