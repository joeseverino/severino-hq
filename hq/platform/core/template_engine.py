"""HQ's template engine: Django's, without the tags and filters that write a
moment in a template's own words.

A page shows a moment through ``when`` alone, so a template that asks for
``date``, ``time``, ``timesince``, ``timeuntil`` or ``{% now %}`` fails as it
compiles: "Invalid filter: 'date'".
"""

from typing import override

from django.template.backends.django import DjangoTemplates
from django.template.library import Library

# What each builtin library loses, by the module Django names it with.
WITHHELD = {
    "django.template.defaultfilters": frozenset({"date", "time", "timesince", "timeuntil"}),
    "django.template.defaulttags": frozenset({"now"}),
}


def _without(library: Library, names: frozenset[str]) -> Library:
    kept = Library()
    kept.tags = {name: tag for name, tag in library.tags.items() if name not in names}
    kept.filters = {name: found for name, found in library.filters.items() if name not in names}
    return kept


class Templates(DjangoTemplates):
    """The ``DjangoTemplates`` backend, its builtins without ``WITHHELD``."""

    # Every engine shares each builtin module's one ``register``
    # (``Engine.get_template_builtins`` through ``library.import_library``),
    # and a parser takes its tags and filters from this engine's list
    # (``Parser.add_library``). So the list holds copies, and the modules stay
    # whole for any other engine: Django's own error page is drawn by one, and
    # it prints a date.
    @override
    def __init__(self, params):
        super().__init__(params)
        engine = self.engine
        engine.template_builtins = [
            _without(library, WITHHELD[path]) if path in WITHHELD else library
            for path, library in zip(engine.builtins, engine.template_builtins, strict=True)
        ]
