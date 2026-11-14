"""The theme a person has chosen: system, light or dark.

A preference about drawing, so it never reaches a spec or the controller. The
page reads it to render `data-theme` on `<html>` server-side, which is what
lets a chosen theme paint on the first frame with no script.
"""

from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from django.db import transaction
from django.utils import timezone

from hq.platform.application.record_errors import InputError
from hq.platform.application.security import Principal, person
from hq.platform.core.models import Appearance

from .command_fields import Choices, Describe
from .describe import listed
from .ui import ended

Theme = Appearance.Theme

SET_THEME = "appearance.theme.set"


def theme_for(user) -> str:
    """The theme to draw for ``user``; anybody signed out follows the system."""

    if user is None or not user.is_authenticated:
        return Theme.SYSTEM
    chosen = Appearance.objects.filter(user=user).values_list("theme", flat=True).first()
    return chosen or Theme.SYSTEM


@dataclass(frozen=True, slots=True)
class ThemeCommand:
    """The theme to draw your pages in."""

    theme: Annotated[str, Choices(Theme.choices), Describe("The theme to draw your pages in.")]


class ThemeSet(TypedDict):
    """The theme your pages are drawn in, as the call left it."""

    ok: Literal[True]
    theme: Annotated[str, Describe(ended(f"The theme now set: {listed(Theme.values)}"))]


def set_theme(command: ThemeCommand, *, principal: Principal) -> ThemeSet:
    """Set, not cycle, so a repeated request cannot undo itself. People only."""

    user = person(principal)
    theme = command.theme
    if theme not in Theme.values:
        raise InputError("Choose system, light or dark.")

    with transaction.atomic():
        row = Appearance.objects.select_for_update().filter(user=user).first() or Appearance(user=user)
        if row.theme == theme:
            return {"ok": True, "theme": theme}
        row.theme = theme
        row.changed_at = timezone.now()
        row.audit_said = f"Theme set to {Theme(theme).label}"
        row.save()
    return {"ok": True, "theme": theme}
