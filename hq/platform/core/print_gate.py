"""A value reaches a page only as text or a number.

Every ``{{ value }}`` a template prints passes through one function of
Django's, ``render_value_in_context``, which turns whatever it is handed into
text with ``str()``. That is how a page came to say "True", "None",
"2026-10-23T09:29:15+00:00" or "{'doc_id': …}": the template printed a value
its builder never put in words. ``install`` puts this gate in front of that
function, for host and extension templates alike, so the type is judged where
it is printed rather than in each template that prints it.

What prints: markup (``SafeData``, or anything with ``__html__``: a form field,
an error list), text, a lazy translation, a number that is not a flag (a str
or int enum member is one of these), and a UUID or a path, each of which has
one written form, as an integer key does. Everything else is refused: a flag,
None, a date or a length of time, a mapping, a sequence, bytes, a plain enum
member, a model instance, any other object. Its builder says it in words
(``moments.when``, ``ui.MISSING``, a count, a name).

The gate holds the templates HQ and its extensions ship. Django's own
templates (a template whose origin is inside the installed Django package) are
the framework's: a date input writes its value as a stamp, and a file input
names its current file by printing the file.

Under test and with DEBUG a refusal raises ``Unprintable``, naming the type.
In production the page prints the mark for a missing value and the refusal is
logged as an error, so one forgotten value never takes a page down.

The filters that turn a value into text (``breakable``, ``readout``,
``or_empty``) and ``payload_words.value_words`` pass what they are given
through ``admitted``, so a ``str()`` there cannot print what the gate refuses.
``{% firstof %}`` and ``{% cycle %}`` (``defaulttags``) and the translation
tags (``i18n``) hold their own reference to the function; ``install`` gates
those too.
"""

import functools
import logging
import os
import re
from contextvars import ContextVar
from enum import Enum
from numbers import Number
from pathlib import Path, PurePath
from uuid import UUID

import django
from django.conf import settings
from django.template import base, defaulttags
from django.templatetags import i18n
from django.utils.functional import Promise

from hq.platform.application.timestamps import moment
from hq.platform.application.ui import MISSING

logger = logging.getLogger("severino.print")

# Where the framework's own templates are read from.
_DJANGO = f"{Path(django.__file__).parent}{os.sep}"
# The extended form a stamp is written in: a date with its dashes, then
# optionally a time and an offset. The basic form ("20261023") is also a
# number, so it is left to read as one.
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?")


# Every node that prints through ``render_value_in_context``: a variable,
# ``{% cycle %}``, ``{% firstof %}`` and the translation tags.
PRINTING = (
    base.VariableNode,
    defaulttags.CycleNode,
    defaulttags.FirstOfNode,
    i18n.TranslateNode,
    i18n.BlockTranslateNode,
)
# The node printing now, which carries its template's origin and its line.
_node: ContextVar[base.Node] = ContextVar("hq_print_node")


class Unprintable(TypeError):
    """A template printed a value that is neither text nor a number."""


def is_stamp(text: str) -> bool:
    """Whether ``text`` is wholly an ISO 8601 date or instant."""

    text = text.strip()
    return bool(_STAMP.fullmatch(text)) and moment(text, naive="keep", zero_is_never=False) is not None


def refusal(value: object) -> str | None:
    """Why ``value`` cannot be printed as it is, or None when it can."""

    if hasattr(value, "__html__") or isinstance(value, Promise):
        return None
    if isinstance(value, bool):
        return "a flag"
    if isinstance(value, str):
        return "an ISO 8601 stamp" if is_stamp(value) else None
    if isinstance(value, (Number, UUID, PurePath)):
        return None
    if value is None:
        return "None"
    if isinstance(value, Enum):
        return "an enum member"
    return f"a {type(value).__module__}.{type(value).__qualname__}"


def strict() -> bool:
    """Whether a refusal raises (under test and with DEBUG) or is logged."""

    return settings.SEVERINO_STRICT_PRINT


def admitted(value, *, where: str = "A template"):
    """``value`` when it may be printed; otherwise refused.

    Refused, it raises ``Unprintable`` where ``strict`` holds, and elsewhere
    is logged and answered with the mark for a missing value. ``where`` names
    what printed it.
    """

    reason = refusal(value)
    if reason is None:
        return value
    said = (
        f"{where} printed {reason} ({value!r:.80}). A page shows text or a number: say it in words where it is built."
    )
    if strict():
        raise Unprintable(said)
    logger.error(said, extra={"event": "print.refused"})
    # Text, not markup: the value may stand in an attribute.
    return MISSING


def _printing(render):
    """``render`` of a node that prints, marking the node while it does."""

    @functools.wraps(render)
    def marked(self, context):
        token = _node.set(self)
        try:
            return render(self, context)
        finally:
            _node.reset(token)

    return marked


def _gated(render):
    @functools.wraps(render)
    def render_value_in_context(value, context):
        # The template that holds the print, not the root of its extends chain.
        node = _node.get()
        origin = node.origin
        if not origin.name.startswith(_DJANGO):
            value = admitted(value, where=f"{origin.template_name or origin.name}:{node.token.lineno}")
        return render(value, context)

    return render_value_in_context


@functools.cache
def install() -> None:
    """Gate every place Django prints a value, once for the life of the process."""

    for node in PRINTING:
        node.render = _printing(node.render)
    gated = _gated(base.render_value_in_context)
    # Each module looks the name up in its own globals when it prints.
    for module in (base, defaulttags, i18n):
        module.render_value_in_context = gated
