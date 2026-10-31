"""Make guessing a password expensive, using the record HQ already keeps.

The password form is the break-glass path beside single sign-on, and so the one
credential in the fleet that verifies a guess rather than a signature, open to
trying as fast as the network allows. A lockout is a reading of the failed
logins the audit log already holds, so the evidence and the enforcement cannot
come apart.
"""

from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone
lazy from django.db.models import Q

from .models import AuditLog


@dataclass(frozen=True, slots=True, kw_only=True)
class Lockout:
    """Whether sign-in is barred right now, and for how much longer."""

    locked: bool
    seconds_remaining: int = 0

    @property
    def minutes_remaining(self) -> int:
        return max(1, -(-self.seconds_remaining // 60))


# No second place knows about failed logins: `user_login_failed` is already
# audited, because an operator needs to see attempts against their own account,
# and a counter table beside it could disagree, the throttle silent because its
# rows were pruned while the audit log showed an attack the whole time.
def _recent_failures(username: str, ip: str):
    window_start = timezone.now() - timezone.timedelta(seconds=settings.SEVERINO_LOGIN_WINDOW_SECONDS)
    failures = AuditLog.objects.filter(action=AuditLog.Action.LOGIN_FAILED, created_at__gte=window_start)
    # Counted per account *and* per source, not per pair. Keying on the two
    # together would leave both halves open: an attacker rotating usernames
    # from one address never trips an account counter, and a password sprayed
    # from many addresses never trips a source counter. Either signal alone is
    # enough to stop serving.
    subject = username.strip().lower()
    if subject and ip:
        failures = failures.filter(Q(metadata__username__iexact=subject) | Q(metadata__ip=ip))
    elif subject:
        failures = failures.filter(metadata__username__iexact=subject)
    elif ip:
        failures = failures.filter(metadata__ip=ip)
    else:
        return AuditLog.objects.none()
    return failures


# The window slides and nothing is written to lift a lock, so it expires by the
# passage of time alone: there is no unlock path to get stuck, and a restart
# cannot forget that an attack is in progress.
def lockout(username: str, ip: str) -> Lockout:
    """Read recent history and decide whether this attempt is allowed."""

    threshold = settings.SEVERINO_LOGIN_MAX_ATTEMPTS
    if threshold <= 0:
        return Lockout(locked=False)
    failures = _recent_failures(username or "", ip or "")
    recent = list(failures.order_by("-created_at").values_list("created_at", flat=True))
    if len(recent) < threshold:
        return Lockout(locked=False)
    # The lock runs from the most recent failure, so continuing to hammer the
    # form extends it rather than waiting it out.
    elapsed = (timezone.now() - recent[0]).total_seconds()
    remaining = settings.SEVERINO_LOGIN_WINDOW_SECONDS - elapsed
    if remaining <= 0:
        return Lockout(locked=False)
    return Lockout(locked=True, seconds_remaining=int(remaining))
