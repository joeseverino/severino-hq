"""The theme a person has chosen: system, light or dark.

A preference about drawing, so it never reaches a spec or the controller. The
page reads it to render `data-theme` on `<html>` server-side, which is what
lets a chosen theme paint on the first frame with no script.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from django.db import transaction
from django.utils import timezone

from hq.platform.application.record_errors import InputError
from hq.platform.application.security import Principal, person
from hq.platform.core.audit import record_event
from hq.platform.core.models import Appearance, AuditLog

from .command_fields import Choices, Describe

Theme = Appearance.Theme
AUDIT_LABEL = "Appearance"

SET_THEME = "appearance.theme.set"


def theme_for(user) -> str:
    """The theme to draw for ``user``; anybody signed out follows the system."""

    if user is None or not user.is_authenticated:
        return Theme.SYSTEM
    chosen = Appearance.objects.filter(user=user).values_list("theme", flat=True).first()
    return chosen or Theme.SYSTEM


@dataclass(frozen=True, slots=True)
class ThemeCommand:
    """The theme to draw this person's pages in."""

    theme: Annotated[str, Choices(Theme.choices), Describe("The theme to draw your pages in.")]


def set_theme(command: ThemeCommand, *, principal: Principal) -> dict[str, Any]:
    """Set, not cycle, so a repeated request cannot undo itself. People only."""

    user = person(principal)
    theme = command.theme
    if theme not in Theme.values:
        raise InputError("Choose system, light or dark.")

    with transaction.atomic():
        row, _ = Appearance.objects.select_for_update().get_or_create(user=user)
        if row.theme == theme:
            return {"ok": True, "theme": theme}
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
    return {"ok": True, "theme": theme}
