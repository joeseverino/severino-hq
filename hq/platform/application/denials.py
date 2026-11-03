"""Refusals, recorded in the audit log.

An authenticated caller's refusal gets its own row. A rejected credential proves
nothing about its sender, so those are counted per surface, source and reason
per minute. An agent refused for asking too much is counted the same way per
agent, in memory between writes, so a flood writes a row a minute rather than
a row a call. The credential itself is never recorded, and recording never
blocks the refusal.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from django.utils import timezone

from hq.platform.core.audit import Unattributed, emit_line, record_event
from hq.platform.core.models import AuditLog
from hq.platform.core.write_bounds import Bound

from .ui import ended
lazy from .capabilities import capability_title

logger = logging.getLogger("severino.denials")

LABEL = "Access denied"
COALESCE_WINDOW = timedelta(seconds=60)
# The metadata keys a coalesced refusal moves after its row is written: how
# many calls it stands for, which only grows, and when the latest came, which
# only moves forward. The audit log's guard (``core.append_only``) lets a
# denied row change these and nothing else.
COUNT = "count"
LAST_AT = "last_at"

REFUSED_CREDENTIALS = Bound(
    "refused credentials",
    "one row per surface, source and reason a minute, counting the rest",
)


def record_denial(
    *,
    interface: str,
    reason: str,
    actor: str = "",
    capability: str = "",
    source: str = "",
    detail: str = "",
    authenticated: bool = True,
) -> None:
    metadata = {
        "interface": interface,
        "actor": actor,
        "reason": reason,
        "capability": capability,
        "source": source,
        "authenticated": authenticated,
    }
    try:
        if not authenticated and _coalesce(interface, reason, source):
            return
        record_event(
            action=AuditLog.Action.DENIED,
            type_label=LABEL,
            message=_message(reason, actor=actor, capability=capability, detail=detail),
            metadata={**metadata, COUNT: 1},
        )
    except Unattributed:
        # A door that bound nobody is a defect, never a failed write to shrug off.
        raise
    except Exception:  # noqa: BLE001 - best effort by contract
        logger.warning(
            "Refusal not recorded: %s on %s",
            reason,
            interface,
            extra={"event": "audit.denial.unrecorded"},
        )


@dataclass(slots=True)
class _Window:
    """One agent's refusals of one kind on one door, counted from ``opened`` for a window."""

    opened: float
    count: int = 1
    written: int = 0
    row: int | None = None


_windows: dict[tuple[str, str, str], _Window] = {}
_windows_lock = threading.Lock()


def record_limited(*, interface: str, reason: str, actor: str, source: str = "") -> None:
    """A refusal for asking too much: one row per agent, door and reason a window, with its count.

    The first refusal in a window writes the row and the rest only count, so
    the row's count is written when the window has closed, by the next refusal
    of the agent or its next admitted call (``settle``).
    """

    key = (interface, actor, reason)
    now = monotonic()
    metadata = {
        "interface": interface,
        "actor": actor,
        "reason": reason,
        "capability": "",
        "source": source,
        "authenticated": True,
    }
    with _windows_lock:
        window = _windows.get(key)
        if window is not None and now - window.opened < COALESCE_WINDOW.total_seconds():
            window.count += 1
            return
        closed = window
        opened = _windows[key] = _Window(now)
    try:
        if closed is not None:
            _write_count(closed)
        row = record_event(
            action=AuditLog.Action.DENIED,
            type_label=LABEL,
            message=_message(reason, actor=actor, capability="", detail=""),
            metadata={**metadata, COUNT: 1},
        )
    except Unattributed:
        raise
    except Exception:  # noqa: BLE001 - best effort by contract
        logger.warning("Refusal not recorded: %s on %s", reason, interface, extra={"event": "audit.denial.unrecorded"})
        return
    with _windows_lock:
        opened.row, opened.written = (row.pk, 1) if row is not None else (None, 0)


def settle(actor: str) -> None:
    """Write the count of each of ``actor``'s closed windows and forget them; asked as it is
    admitted, so a window nothing refuses after still gets its count."""

    now = monotonic()
    with _windows_lock:
        if not _windows:
            return
        closed = [
            key
            for key, window in _windows.items()
            if key[1] == actor and now - window.opened >= COALESCE_WINDOW.total_seconds()
        ]
        windows = [_windows.pop(key) for key in closed]
    for window in windows:
        try:
            _write_count(window)
        except Exception:  # noqa: BLE001 - best effort by contract
            logger.warning("Refusal count not recorded", extra={"event": "audit.denial.unrecorded"})


def _write_count(window: _Window) -> None:
    if window.row is None or window.count <= window.written:
        return
    row = AuditLog.objects.filter(pk=window.row).first()
    if row is not None:
        row.metadata = {**row.metadata, COUNT: window.count}
        row.save(update_fields=["metadata"])
        _recounted(row)


def _coalesce(interface: str, reason: str, source: str) -> bool:
    recent = (
        AuditLog.objects.filter(
            action=AuditLog.Action.DENIED,
            object_type=LABEL,
            created_at__gte=timezone.now() - COALESCE_WINDOW,
            metadata__authenticated=False,
            metadata__interface=interface,
            metadata__reason=reason,
            metadata__source=source,
        )
        .order_by("-id")
        .first()
    )
    if recent is None:
        return False
    recent.metadata = {
        **recent.metadata,
        COUNT: int(recent.metadata.get(COUNT, 1)) + 1,
        LAST_AT: timezone.now().isoformat(),
    }
    recent.save(update_fields=["metadata"])
    _recounted(recent)
    return True


def _recounted(row: AuditLog) -> None:
    """The log line outside HQ follows the row's count, as it follows the row."""

    emit_line("audit.recounted", {"id": row.pk, COUNT: row.metadata.get(COUNT), LAST_AT: row.metadata.get(LAST_AT)})


def _message(reason: str, *, actor: str, capability: str, detail: str) -> str:
    who = actor or "An unauthenticated caller"
    what = f" “{capability_title(capability)}”" if capability else ""
    said = f"{who} was refused{what}: {reason.replace('_', ' ')}"
    return f"{ended(said)} {detail}".strip() if detail else said
