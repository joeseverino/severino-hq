"""A value a page shows is words, refused at construction when it is not.

Every view model a caller hands HQ to draw is declared with ``@display``, a
frozen dataclass whose ``__post_init__`` runs ``said_in_words`` first, so a
stamp, a dumped record or a field's name fails where it was built, with the
builder in the traceback, and never reaches a page.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from typing import Any, dataclass_transform

# What only a machine writes, by what it is.
MACHINE_TEXT = {
    "a date-time stamp": re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|\A\d{4}-\d{2}-\d{2}\Z"),
    "a dumped record": re.compile(r"\{'[^']*': "),
    "a dumped list": re.compile(r"\[(?:'[^']*'|\{)"),
    "a bare flag": re.compile(r"(?<![\w-])(?:True|False)(?![\w-])"),
    "markup": re.compile(r"<[a-z][\w-]*[\s/>]|(?<![\w-])[a-z][\w-]*=\"[^\"]*\""),
}
# A field's name as code writes it, where its label belongs.
_KEY = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def _machine_text(text: str, *, label: bool = False) -> str:
    """What a machine wrote in ``text``, named, or "" when it is words.

    A ``label`` (a heading, a fact's name) is also refused as a snake_case key.
    """

    for name, shape in MACHINE_TEXT.items():
        if found := shape.search(text):
            return f"{name}, {found.group(0)!r}"
    return "a field's name" if label and _KEY.fullmatch(text) else ""


def in_words(text: str, *, label: bool = False) -> str:
    """``text``, or ValueError naming what a machine wrote in it."""

    if found := _machine_text(text, label=label):
        raise ValueError(f"{text!r} carries {found}: say it in words.")
    return text


def said_in_words(view: Any) -> None:
    """Refuse a view model whose text a machine wrote, or a value that is not text.

    A field typed ``str`` holds a str, a flag is only a field typed ``bool``,
    and each str a field holds, in a tuple too, is words. A field named
    ``label`` or ``…_label`` is a label.
    """

    for field in fields(view):
        value = getattr(view, field.name)
        where = f"{type(view).__name__}.{field.name}"
        if (field.type is str and not isinstance(value, str)) or (isinstance(value, bool) and field.type is not bool):
            raise ValueError(f"{where} is words; got {value!r}.")
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
