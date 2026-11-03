"""What a caller who has not signed in can make HQ write, declared where the code that writes is.

A route that answers without a session (``speculation.sessionless_views``), and
every middleware of HQ's that runs before the sign-in gate decides, is declared
with ``writes``: as writing nothing for such a caller, or under one or more
named ``Bound`` s, each the mechanism that keeps what it writes finite however
many requests arrive. ``test_write_bounds`` fails on one that declares nothing.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.utils import timezone

lazy from .models import AuditLog

_ATTRIBUTE = "writes_under"


@dataclass(frozen=True, slots=True)
class Bound:
    """A named limit on what a caller without a session can make HQ write."""

    name: str
    says: str


NOTHING = Bound("nothing", "writes nothing for a caller who has not signed in")


@dataclass(frozen=True, slots=True)
class RowCap(Bound):
    """At most ``rows`` audit rows of one kind a ``window``, however many distinct values arrive."""

    action: str = ""
    type_label: str = ""
    rows: int = 0
    window: timedelta = timedelta(hours=1)

    def admits(self, **metadata: Any) -> bool:
        """Whether one more row fits, among those whose metadata matches ``metadata``."""

        written = AuditLog.objects.filter(
            action=self.action,
            object_type=self.type_label,
            created_at__gte=timezone.now() - self.window,
            **{f"metadata__{key}": value for key, value in metadata.items()},
        )
        return written[: self.rows].count() < self.rows


def writes[T: Callable[..., Any] | type](*bounds: Bound) -> Callable[[T], T]:
    """Declare what the view, view class or middleware it decorates writes for a caller without a session."""

    if not bounds or not all(isinstance(bound, Bound) for bound in bounds):
        raise TypeError("writes() names NOTHING or at least one Bound.")

    def declare(target: T) -> T:
        setattr(target, _ATTRIBUTE, bounds)
        return target

    return declare


def declared(target: Any) -> tuple[Bound, ...] | None:
    """The bounds ``target`` declares (a view, its class, a middleware), or None for none."""

    found = getattr(target, _ATTRIBUTE, None)
    if found is None:
        found = getattr(getattr(target, "view_class", None), _ATTRIBUTE, None)
    return found
