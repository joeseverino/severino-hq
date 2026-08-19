"""Wire up login/logout audit events."""

from __future__ import annotations

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

from .audit import record_event
from .models import AuditLog


@receiver(user_logged_in)
def _on_login(sender, user, request, **kwargs):
    record_event(
        action=AuditLog.Action.LOGIN,
        obj=user,
        type_label="User",
        message=f"{user} signed in",
        user=user,
    )


@receiver(user_logged_out)
def _on_logout(sender, user, request, **kwargs):
    if user is None:
        return
    record_event(
        action=AuditLog.Action.LOGOUT,
        obj=user,
        type_label="User",
        message=f"{user} signed out",
        user=user,
    )


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    """Record the attempt, including where it came from.

    The source address is not decoration. `core.throttle` reads these rows back
    to decide whether sign-in is currently barred, so an attempt logged without
    one is an attempt the lockout cannot count -- a password sprayed across
    many usernames would leave nothing for the per-source rule to see.
    """

    from .network import client_ip

    username = credentials.get("username", "") if credentials else ""
    ip = client_ip(request) if request is not None else ""
    record_event(
        action=AuditLog.Action.LOGIN_FAILED,
        type_label="User",
        message=f"Failed login attempt for {username!r}",
        metadata={"username": username, "ip": ip},
    )
