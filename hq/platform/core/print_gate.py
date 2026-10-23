"""Every value a template of HQ or one of its extensions prints is held to
``application.printable``.

Django prints a ``{{ value }}``, ``{% cycle %}``, ``{% firstof %}`` and the
translation tags through ``render_value_in_context``, which each module looks
up in its own globals, and an escaped ``simple_tag``'s result through
``conditional_escape`` in ``template.library``. ``install`` gates those names,
and marks each node that prints through them while it renders, so the gate
knows the template and line that printed.

A template is held when its origin lies in a template directory of HQ or of an
installed extension app. Every other template is a third party's: Django's
widgets print a date input's value as a stamp, the debug toolbar prints raw
settings, and a template built from a string (Django's own error pages) has no
directory at all.
"""

import functools
import os
from contextvars import ContextVar
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.template import base, defaulttags, library
from django.templatetags import i18n

from hq.platform.application.plugins import installed_plugin_apps
from hq.platform.application.printable import refusal, refused

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
# The node printing now, which carries its template's origin and its line.
_node: ContextVar[base.Node] = ContextVar("hq_print_node")


@functools.cache
def _ours() -> tuple[str, ...]:
    """The template directories of HQ and its installed extension apps."""

    extensions = set(installed_plugin_apps())
    directories = [
        *(directory for backend in settings.TEMPLATES for directory in backend.get("DIRS", ())),
        *(
            Path(config.path) / "templates"
            for config in apps.get_app_configs()
            if config.name.startswith("hq.") or config.name in extensions
        ),
    ]
    return tuple(f"{Path(directory)}{os.sep}" for directory in directories)


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


def _gated(write):
    """``write``, refusing what the node printing now may not print."""

    @functools.wraps(write)
    def gated(value, *args):
        reason = refusal(value)
        if reason is not None:
            # The template that holds the print, not the root of its extends chain.
            node = _node.get(None)
            origin = getattr(node, "origin", None)
            if origin is not None and origin.name.startswith(_ours()):
                value = refused(reason, f"{origin.template_name or origin.name}:{node.token.lineno}")
        return write(value, *args)

    gated.hq_print_gate = True
    return gated


def install() -> None:
    """Gate every place Django prints a value; a second call changes nothing."""

    if getattr(base.render_value_in_context, "hq_print_gate", False):
        return
    for node in PRINTING:
        node.render = _printing(node.render)
    gated = _gated(base.render_value_in_context)
    for module in (base, defaulttags, i18n):
        module.render_value_in_context = gated
    library.conditional_escape = _gated(library.conditional_escape)
