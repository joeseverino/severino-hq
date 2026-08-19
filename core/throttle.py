"""Make guessing a password expensive, using the record HQ already keeps.

Single sign-on through Pocket ID is how a person signs in. The password form
stays as the break-glass path for the day the identity provider is the thing
that is broken -- which means it is also the one credential in the fleet that
can be attacked by simply trying, over and over, as fast as the network allows.
Everything else in HQ verifies a signature; this verifies a guess.

Note what is *not* introduced here: a second place that knows about failed
logins. `user_login_failed` is already recorded to the audit log, because an
operator has always needed to see attempts against their own account. A
counter table beside it would hold the same fact twice and let the two disagree
-- the throttle silent because its rows were pruned, the audit log showing an
attack the whole time. Deriving the lockout from the audit trail means the
evidence and the enforcement cannot come apart: whatever the log shows is
exactly what the gate acted on.

The window slides and nothing is written to lift a lock. A lock is not a state
that gets set and cleared; it is a reading of recent history, so it expires by
the passage of time alone. There is no unlock path to get stuck, and a
restart cannot forget that an attack is in progress.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from .models import AuditLog


@dataclass(frozen=True)
class Lockout:
    """Whether sign-in is barred right now, and for how much longer."""

    locked: bool
    seconds_remaining: int = 0

    @property
    def minutes_remaining(self) -> int:
        return max(1, -(-self.seconds_remaining // 60))


def _recent_failures(username: str, ip: str):
    window_start = timezone.now() - timezone.timedelta(
        seconds=settings.SEVERINO_LOGIN_WINDOW_SECONDS
    )
    failures = AuditLog.objects.filter(
        action=AuditLog.Action.LOGIN_FAILED, created_at__gte=window_start
    )
    # Counted per account *and* per source, not per pair. Keying on the two
    # together would leave both halves open: an attacker rotating usernames
    # from one address never trips an account counter, and a password sprayed
    # from many addresses never trips a source counter. Either signal alone is
    # enough to stop serving.
    subject = username.strip().lower()
    if subject and ip:
        from django.db.models import Q

        failures = failures.filter(
            Q(metadata__username__iexact=subject) | Q(metadata__ip=ip)
        )
    elif subject:
        failures = failures.filter(metadata__username__iexact=subject)
    elif ip:
        failures = failures.filter(metadata__ip=ip)
    else:
        return AuditLog.objects.none()
    return failures


def lockout(username: str, ip: str) -> Lockout:
    """Read recent history and decide whether this attempt is allowed."""

    threshold = settings.SEVERINO_LOGIN_MAX_ATTEMPTS
    if threshold <= 0:
        return Lockout(False)
    failures = _recent_failures(username or "", ip or "")
    recent = list(failures.order_by("-created_at").values_list("created_at", flat=True))
    if len(recent) < threshold:
        return Lockout(False)
    # The lock runs from the most recent failure, so continuing to hammer the
    # form extends it rather than waiting it out.
    elapsed = (timezone.now() - recent[0]).total_seconds()
    remaining = settings.SEVERINO_LOGIN_WINDOW_SECONDS - elapsed
    if remaining <= 0:
        return Lockout(False)
    return Lockout(True, int(remaining))
