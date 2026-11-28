"""The icon part's one reference into static/img/icons.svg, which refuses a
name the sheet draws no symbol for, so no icon draws blank."""

import re
from functools import cache
from pathlib import Path
from typing import Annotated

from django import template
from django.contrib.staticfiles import finders
from django.template import TemplateSyntaxError

from hq.platform.core.template_literals import Reads, reading

register = template.Library()

SHEET = "img/icons.svg"


@cache
def symbols() -> frozenset[str]:
    """Each symbol the sheet draws, by its id."""

    drawn = Path(finders.find(SHEET)).read_text(encoding="utf-8")
    return frozenset(re.findall(r'<symbol\s+id="([^"]+)"', drawn))


def drawn(name: str) -> str:
    """``name``, when the sheet draws it."""

    if name not in symbols():
        raise TemplateSyntaxError(f"{name!r} is no symbol of {SHEET}: name one it draws, or draw it there.")
    return name


@register.filter
@reading
def symbol(name: Annotated[str, Reads(drawn)]) -> str:
    """``name``, a symbol the sheet draws."""

    return name
