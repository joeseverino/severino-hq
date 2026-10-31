"""What a page may print: text, markup or a number.

Django prints a value with ``str()``, so a value its builder never put in words
reaches the page as Python spells it. Markup, text, a number that is not a flag,
a UUID and a path print; anything else is refused, and its builder says it in
words (``moments.when``, ``ui.MISSING``, a count, a name). How a refusal is
answered in each setting: docs/Plugins.md#what-a-template-prints.
"""

import logging
from enum import Enum
from numbers import Number
from pathlib import PurePath
from typing import Any
from uuid import UUID

from django.conf import settings
from django.utils.functional import Promise

from .ui import MISSING, ended

logger = logging.getLogger("severino.print")


class Unprintable(TypeError):
    """A page was handed a value that is neither text nor a number."""


def refusal(value: object) -> str | None:
    """Why ``value`` cannot be printed as it is, or None when it can."""

    # Text first: nearly every value a page prints is a str.
    if isinstance(value, (str, Promise)) or hasattr(value, "__html__"):
        return None
    if isinstance(value, bool):
        return "a flag"
    # Each has one written form; a str or int enum member is one of these.
    if isinstance(value, (Number, UUID, PurePath)):
        return None
    if value is None:
        return "None"
    kind = f"{type(value).__module__}.{type(value).__qualname__}"
    return f"an enum member ({kind})" if isinstance(value, Enum) else f"a {kind}"


def refused(reason: str, where: str) -> str:
    """The mark for a missing value, in place of one ``where`` could not print;
    raises ``Unprintable`` instead where strict printing holds."""

    said = f"{where} printed {ended(reason)} A page shows text or a number: say it in words where it is built."
    if settings.SEVERINO_STRICT_PRINT:
        raise Unprintable(said)
    logger.error(said, extra={"event": "print.refused"})
    # Text, not markup: the value may stand in an attribute.
    return MISSING


def admitted(value: Any, where: str = "A template") -> Any:
    """``value`` when it may be printed; otherwise what ``refused`` makes of it."""

    reason = refusal(value)
    return value if reason is None else refused(reason, where)
