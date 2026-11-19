"""HQ's template engine: Django's, without the tags and filters that write a
moment in a template's own words, and with loaders that refuse a shape a
shared part draws, written by hand.

A page shows a moment through ``when`` alone, so a template that asks for
``date``, ``time``, ``timesince``, ``timeuntil`` or ``{% now %}`` fails as it
compiles: "Invalid filter: 'date'". An element a shared part draws
(``DRAWN``) is written only in the templates of a part that declares it
(``Part.elements``), so a template of HQ or an extension that writes one
anywhere else fails as it loads.
"""

import functools
import os
import re
from pathlib import Path
from typing import override

from django.template import TemplateSyntaxError
from django.template.backends.django import DjangoTemplates
from django.template.base import Lexer, Origin, TokenType
from django.template.library import Library
from django.template.loaders import app_directories, base, filesystem

lazy from django.apps import apps
lazy from django.conf import settings

lazy from hq.platform.application.plugins import installed_plugin_apps
lazy from hq.platform.core.template_contract import parts

# What each builtin library loses, by the module Django names it with.
WITHHELD = {
    "django.template.defaultfilters": frozenset({"date", "time", "timesince", "timeuntil"}),
    "django.template.defaulttags": frozenset({"now"}),
}

# Each element a shared part draws, and how a page writes that part instead.
DRAWN = {
    "details": '{% section "Heading" fold=True %}',
}

_START = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)")


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


def ours() -> tuple[str, ...]:
    """The template directories of HQ and its installed extension apps.

    Read at each use: the installed set and the apps it names are whatever is
    composed then.
    """

    # A manifest may name an app by its module or by its AppConfig.
    extensions = {apps.get_containing_app_config(app).name for app in installed_plugin_apps()}
    directories = [
        *(directory for backend in settings.TEMPLATES for directory in backend.get("DIRS", ())),
        *(
            Path(config.path) / "templates"
            for config in apps.get_app_configs()
            if config.name.startswith("hq.") or config.name in extensions
        ),
    ]
    return tuple(f"{Path(directory)}{os.sep}" for directory in directories)


@functools.cache
def _drawn_in() -> dict[str, frozenset[str]]:
    """The elements of ``DRAWN`` each part's template writes, by its name."""

    drawn: dict[str, frozenset[str]] = {}
    for part in parts():
        for name in part.files():
            drawn[name] = drawn.get(name, frozenset()) | frozenset(part.elements)
    return drawn


def hand_drawn(source: str, name: str) -> list[tuple[int, str]]:
    """Each element of ``DRAWN`` the template ``name`` writes outside a part
    that declares it, by line. Read from Django's own tokens, so a
    ``{% comment %}``, which no page receives, holds none."""

    own = _drawn_in().get(name, frozenset())
    found: list[tuple[int, str]] = []
    commented = False
    for token in Lexer(source).tokenize():
        if token.token_type is TokenType.BLOCK:
            command = token.contents.partition(" ")[0]
            commented = command == "comment" or (commented and command != "endcomment")
        elif token.token_type is TokenType.TEXT and not commented:
            for match in _START.finditer(token.contents):
                element = match.group(1).lower()
                if element in DRAWN and element not in own:
                    line = token.lineno + token.contents.count("\n", 0, match.start())
                    found.append((line, f"<{element}> is a shared part's: write {DRAWN[element]}"))
    return found


class _Refusing(base.Loader):
    """A loader whose templates of HQ and its extensions draw no shared
    part's shape by hand (``hand_drawn``)."""

    @override
    def get_contents(self, origin: Origin) -> str:
        contents = super().get_contents(origin)
        name = os.fsdecode(origin.template_name)
        if origin.name.startswith(ours()) and (found := hand_drawn(contents, name)):
            raise TemplateSyntaxError("; ".join(f"{name}:{line}: {said}" for line, said in found))
        return contents


class FilesystemLoader(_Refusing, filesystem.Loader):
    """Django's filesystem loader, refusing a shape drawn by hand."""


class AppDirectoriesLoader(_Refusing, app_directories.Loader):
    """Django's app directories loader, refusing a shape drawn by hand."""
