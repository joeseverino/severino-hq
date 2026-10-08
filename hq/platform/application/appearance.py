"""The theme a person has chosen: system, light or dark.

A preference about drawing, so it never reaches a spec or the controller. The
page reads it to render `data-theme` on `<html>` server-side, which is what
lets a chosen theme paint on the first frame with no script.
"""

from django.db import transaction
from django.utils import timezone

from hq.platform.application.security import AuthorizationError, Principal, is_interactive
from hq.platform.core.audit import record_event
from hq.platform.core.models import Appearance, AuditLog

Theme = Appearance.Theme
AUDIT_LABEL = "Appearance"


def theme_for(user) -> str:
    """The theme to draw for ``user``; anybody signed out follows the system."""

    if not getattr(user, "is_authenticated", False):
        return Theme.SYSTEM
    chosen = Appearance.objects.filter(user=user).values_list("theme", flat=True).first()
    return chosen or Theme.SYSTEM


def set_theme(theme: str, *, principal: Principal, user) -> str:
    """Set, not cycle, so a repeated request cannot undo itself. People only."""

    if not is_interactive(principal):
        raise AuthorizationError(
            f"{principal.interface} principal {principal.actor!r} cannot choose a theme."
        )
    if theme not in Theme.values:
        raise ValueError(f"theme must be one of {', '.join(Theme.values)}")

    with transaction.atomic():
        row, _ = Appearance.objects.select_for_update().get_or_create(user=user)
        if row.theme == theme:
            return theme
        row.theme = theme
        row.changed_at = timezone.now()
        row.save(update_fields=["theme", "changed_at"])
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=row,
            type_label=AUDIT_LABEL,
            message=f"Theme set to {Theme(theme).label}",
            user=user,
            required=True,
        )
    return theme
