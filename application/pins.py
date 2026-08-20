"""What an operator wants at the top, and nothing else about it.

A pin is an ordering preference, so it is read as a set of keys and applied
where things are listed. It never reaches a spec, a generation or the
controller: the world does not change because somebody starred a domain.
"""

from __future__ import annotations

from core.models import Pin

DOMAIN = "domain"


def pinned(user, target_kind: str) -> frozenset[str]:
    """Keys this operator has pinned, lowercased for comparison."""

    if not getattr(user, "is_authenticated", False):
        return frozenset()
    return frozenset(
        key.lower()
        for key in Pin.objects.filter(user=user, target_kind=target_kind).values_list(
            "target_key", flat=True
        )
    )


def toggle(user, target_kind: str, target_key: str) -> bool:
    """Pin or unpin, returning whether it is pinned afterwards."""

    key = str(target_key).strip().lower()
    if not key:
        return False
    existing = Pin.objects.filter(user=user, target_kind=target_kind, target_key=key)
    if existing.exists():
        existing.delete()
        return False
    Pin.objects.create(user=user, target_kind=target_kind, target_key=key)
    return True
