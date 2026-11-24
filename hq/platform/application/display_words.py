"""A value a page shows is words, refused at construction when it is not.

Every view model a caller hands HQ to draw is declared with ``@display``, a
frozen dataclass whose ``__post_init__`` runs ``said_in_words`` first, so a
stamp, a dumped record or a field's name fails where it was built, with the
builder in the traceback, and never reaches a page.
"""

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, fields
from enum import Enum
from functools import cache
from types import MappingProxyType, UnionType
from typing import Any, Literal, Union, dataclass_transform, get_args, get_origin

from django.db import models

# What only a machine writes, by what it is.
MACHINE_TEXT = {
    "a date-time stamp": re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|\A\d{4}-\d{2}-\d{2}\Z"),
    "a dumped record": re.compile(r"\{'[^']*': "),
    "a dumped list": re.compile(r"\[(?:'[^']*'|\{)"),
    "a bare flag": re.compile(r"(?<![\w-])(?:True|False)(?![\w-])"),
    "markup": re.compile(r"<[a-z][\w-]*[\s/>]|(?<![\w-])[a-z][\w-]*=\"[^\"]*\""),
}
# A field's metadata when it quotes a record's own text (a search match): a
# person or a provider wrote it, and a page shows it as they did.
QUOTED: Mapping[str, bool | str] = MappingProxyType({"quoted": True})


def quoted_when(flag: str) -> Mapping[str, bool | str]:
    """``QUOTED`` whenever its view's ``flag`` is set: a readout's words."""

    return MappingProxyType({"quoted": flag})


class Quoted(str):
    """Text a person wrote outside HQ, read where HQ reads it (a playbook's
    title and statements, an input's description, a scan's problems): the
    author's own words, shown as written however they read, and escaped on
    the page like any text. Joined only with more of them it is wrapped
    again; beside HQ's own words it is a field or item of its own, never
    one str with them.
    """

    __slots__ = ()


class QuotedCharField(models.CharField):
    """A column of text a person wrote outside HQ (a vault doc's title, a
    writeup's), read back from the database as ``Quoted``."""

    def from_db_value(self, value: str | None, _expression: Any, _connection: Any) -> str | None:
        return None if value is None else Quoted(value)

    def deconstruct(self) -> Any:
        # Stored as any CharField: how it is read back changes no column.
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.CharField", args, kwargs


# A field's name as code writes it, where its label belongs.
_KEY = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def _machine_text(text: str, *, label: bool = False) -> str:
    """What a machine wrote in ``text``, named, or "" when it is words.

    A ``label`` (a heading, a fact's name) is also refused as a snake_case key.
    What a person wrote outside HQ (``Quoted``) is words.
    """

    if isinstance(text, Quoted):
        return ""
    for name, shape in MACHINE_TEXT.items():
        if found := shape.search(text):
            return f"{name}, {found.group(0)!r}"
    return "a field's name" if label and _KEY.fullmatch(text) else ""


def in_words(text: str, *, label: bool = False) -> str:
    """``text``, or ValueError naming what a machine wrote in it."""

    if found := _machine_text(text, label=label):
        raise ValueError(f"{text!r} carries {found}: say it in words.")
    return text


@cache
def _members_of(kind: type) -> tuple[tuple[str, type[Enum], bool], ...]:
    """Each field of ``kind`` typed by an enum: its name, the enum, and
    whether it may be empty instead (typed ``Tone | Literal[""]``)."""

    typed = []
    for field in fields(kind):
        if isinstance(field.type, type) and issubclass(field.type, Enum):
            typed.append((field.name, field.type, False))
            continue
        options = get_args(field.type) if isinstance(field.type, UnionType) or get_origin(field.type) is Union else ()
        enums = [option for option in options if isinstance(option, type) and issubclass(option, Enum)]
        if len(enums) == 1 and len(options) == 2 and Literal[""] in options:
            typed.append((field.name, enums[0], True))
    return tuple(typed)


def as_members(view: Any) -> None:
    """Hold each field of ``view`` typed by an enum to one of its members.

    A member's value (``"good"`` for ``Tone.GOOD``) is taken and turned into
    the member as the view is built, so a caller may write either, and a word
    the enum does not have fails where it was written.
    """

    for name, enum, or_empty in _members_of(type(view)):
        value = getattr(view, name)
        if isinstance(value, enum) or (or_empty and value == ""):
            continue
        try:
            member = enum(value)
        except ValueError:
            empty = " or empty" if or_empty else ""
            raise ValueError(
                f"{type(view).__name__}.{name} is one of {', '.join(map(str, enum))}{empty}; got {value!r}."
            ) from None
        object.__setattr__(view, name, member)


def said_in_words(view: Any) -> None:
    """Refuse a view model whose text a machine wrote, or a value that is not text.

    A field typed ``str`` holds a str, a flag is only a field typed ``bool``,
    and each str a field holds, in a tuple too, is words. A field named
    ``label`` or ``…_label`` is a label. A field marked ``QUOTED``, or
    ``quoted_when`` its flag is set, holds a record's own text, shown as it
    was written, so it is not held to words, nor is ``Quoted`` text in any
    field. A field typed by an enum holds its member (``as_members``).
    """

    as_members(view)
    for field in fields(view):
        value = getattr(view, field.name)
        where = f"{type(view).__name__}.{field.name}"
        if (field.type is str and not isinstance(value, str)) or (isinstance(value, bool) and field.type is not bool):
            raise ValueError(f"{where} is words; got {value!r}.")
        quoted = field.metadata.get("quoted", False)
        if quoted is True or (quoted and getattr(view, quoted)):
            continue
        for text, holder in _texts(value, value):
            if found := _machine_text(text, label=field.name.endswith("label")):
                raise ValueError(f"{where} carries {found}: say it in words. Got {holder!r}.")


def _texts(value: Any, holder: Any) -> Iterator[tuple[str, Any]]:
    """Each str in ``value``, with the innermost tuple that holds it."""

    if isinstance(value, str):
        yield value, holder
    elif isinstance(value, tuple):
        for item in value:
            yield from _texts(item, value)


@dataclass_transform(frozen_default=True)
def display[T](cls: type[T] | None = None, /, *, kw_only: bool = False) -> Any:
    """A frozen dataclass whose values are said in words: slotted, or
    keyword-only for a base whose subclasses add required fields."""

    def make(cls: type[T]) -> type[T]:
        own = cls.__dict__.get("__post_init__")
        # A subclass of a display type inherits its check.
        if own is not None or not hasattr(cls, "__post_init__"):
            cls.__post_init__ = _checked(own)
        return dataclass(frozen=True, slots=not kw_only, kw_only=kw_only)(cls)

    return make if cls is None else make(cls)


def _checked(own: Callable[[Any], None] | None) -> Callable[[Any], None]:
    def __post_init__(self: Any) -> None:
        said_in_words(self)
        if own is not None:
            own(self)

    return __post_init__
