"""Every value a template of HQ or one of its extensions prints is held to
``application.printable``.

``install`` gates the names Django prints through. A template outside the
template directories of HQ and its installed extensions is a third party's
(Django's widgets, the debug toolbar, an error page built from a string) and
prints as it always has (docs/Plugins.md, What a template prints).
"""

import functools
import inspect

from django.template import base, defaulttags, library
from django.templatetags import i18n

from hq.platform.application.printable import refusal, refused
from hq.platform.core.template_engine import ours

# Every node that prints a value: through ``render_value_in_context``, or a
# ``simple_tag`` through ``conditional_escape``.
PRINTING = (
    base.VariableNode,
    defaulttags.CycleNode,
    defaulttags.FirstOfNode,
    i18n.TranslateNode,
    i18n.BlockTranslateNode,
    library.SimpleNode,
)
# The code of each printing node's render, by which a refusal finds its node.
_RENDERS = frozenset(node.render.__code__ for node in PRINTING)


def _printing() -> base.Node | None:
    """The innermost node printing now: the ``self`` of the nearest printing
    node's ``render`` on the stack, or None for a print outside any node."""

    # Past this function and the gate that calls it.
    frame = inspect.currentframe().f_back.f_back
    while frame is not None and frame.f_code not in _RENDERS:
        frame = frame.f_back
    return None if frame is None else frame.f_locals["self"]


def _gated(write):
    """``write``, refusing what the node printing now may not print."""

    # A value printed is one type check; only a refused one looks up the stack.
    @functools.wraps(write)
    def gated(value, *args):
        reason = refusal(value)
        if reason is not None:
            # The template that holds the print, not the root of its extends chain.
            node = _printing()
            origin = getattr(node, "origin", None)
            if origin is not None and origin.name.startswith(ours()):
                value = refused(reason, f"{origin.template_name or origin.name}:{node.token.lineno}")
        return write(value, *args)

    gated.hq_print_gate = True
    return gated


def install() -> None:
    """Gate every place Django prints a value; a second call changes nothing.

    Each module looks ``render_value_in_context`` up in its own globals, and an
    escaped ``simple_tag``'s result goes through ``conditional_escape``.
    """

    if getattr(base.render_value_in_context, "hq_print_gate", False):
        return
    gated = _gated(base.render_value_in_context)
    for module in (base, defaulttags, i18n):
        module.render_value_in_context = gated
    library.conditional_escape = _gated(library.conditional_escape)
