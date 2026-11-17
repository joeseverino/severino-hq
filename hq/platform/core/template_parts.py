"""A shared part a template writes as a tag, declared once as a ``Part``,
and the one call that registers it (``slotted``)."""

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from itertools import chain
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from django import template
from django.template.base import token_kwargs
from django.utils.safestring import mark_safe

from hq.platform.application.describe import Describe


class SlotsNode(template.Node):
    """A block tag drawn by its partial, each part it encloses handed over as
    the variable it is named for (``slotted``)."""

    def __init__(self, part: Part, parts: dict[str, template.NodeList], options: dict) -> None:
        self.part = part
        self.options = options
        self.child_nodelists = tuple(f"nodelist_{name}" for name in parts)
        for name, nodelist in parts.items():
            setattr(self, f"nodelist_{name}", nodelist)

    def render(self, context) -> str:
        values = self.part.values({name: value.resolve(context) for name, value in self.options.items()})
        for attribute in self.child_nodelists:
            # Rendered by the page's own template, so escaped already. A part
            # left empty leaves an option of its name as it is.
            drawn = mark_safe(getattr(self, attribute).render(context).strip())  # noqa: S308 - the template's own escaped output
            name = attribute.removeprefix("nodelist_")
            if drawn or name not in values:
                values[name] = drawn
        return context.template.engine.get_template(self.part.partial).render(context.new(values)).strip()


@dataclass(frozen=True, kw_only=True)
class Option:
    """One option a part takes: the value it has when a page gives none, the
    values it may be given (any, when there are no ``choices``), and what it
    does. A ``required`` one has no default and is always written."""

    default: object = ""
    choices: tuple[object, ...] = ()
    about: str = ""
    required: bool = False

    def __post_init__(self) -> None:
        if self.choices and not self.required and self.default not in self.choices:
            raise ValueError(f"An option's default {self.default!r} is one of its choices.")


def choices_of(annotation: Any) -> tuple[object, ...]:
    """The values a field or parameter typed ``annotation`` holds, when the
    type names them all (a ``Literal``, an enum, a union of those); else none."""

    if get_origin(annotation) is Annotated:
        return choices_of(get_args(annotation)[0])
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return tuple(member.value for member in annotation)
    if isinstance(annotation, UnionType) or get_origin(annotation) is Union:
        each = [choices_of(member) for member in get_args(annotation)]
        return tuple(chain.from_iterable(each)) if all(each) else ()
    return ()


def about_of(annotation: Any) -> str:
    """What an ``Annotated`` type's ``Describe`` says it is; else nothing."""

    if get_origin(annotation) is not Annotated:
        return ""
    return next((marker.text for marker in get_args(annotation)[1:] if isinstance(marker, Describe)), "")


def _read_options(function: Callable) -> tuple[dict[str, Option], dict[str, Option]]:
    """A function tag's options, read from its signature: each parameter
    with no default, in order, then the rest by name (``*name`` and
    ``**name`` for what it gathers), each with its default, the choices its
    type names and what its ``Describe`` says. The context and the enclosed
    content are the tag's to hand over, never a page's to give."""

    hints = get_type_hints(function, include_extras=True)
    ordered: dict[str, Option] = {}
    options: dict[str, Option] = {}
    for index, parameter in enumerate(inspect.signature(function).parameters.values()):
        if index == 0 and parameter.name in ("context", "content"):
            continue
        hint = hints.get(parameter.name)
        about, choices = about_of(hint), choices_of(hint)
        required = parameter.default is inspect.Parameter.empty
        default = "" if required else parameter.default
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            ordered[f"*{parameter.name}"] = Option(about=about)
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            options[f"**{parameter.name}"] = Option(about=about)
        elif required and parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            ordered[parameter.name] = Option(choices=choices, about=about, required=True)
        else:
            options[parameter.name] = Option(default=default, choices=choices, about=about, required=required)
    return ordered, options


type Shape = Callable[[str, dict], dict]


@dataclass(frozen=True, kw_only=True)
class Part:
    """A shared part a template writes as a tag, declared once: what it is,
    the partial that draws it, the parts it encloses (the first follows the
    opening tag), what it takes in order and as options, whether a page may
    give it ``data_*`` options, and the shape that turns what a page gave into
    the partial's variables. Everything else said about the tag (its
    parameters in the template contract, its row in docs/Shared-UI.md, the
    check of what a page gave) is read from here. ``public`` tags are an
    extension's to write too, and show themselves written in ``example``.

    A tag drawn by a Python ``function`` (a simple tag, or with a
    ``partial`` an inclusion tag) takes the options its signature declares,
    so the Part says only what a signature cannot."""

    name: str
    about: str
    partial: str = ""
    function: Callable | None = None
    parts: tuple[str, ...] = ("content",)
    ordered: Mapping[str, Option] = field(default_factory=dict)
    options: Mapping[str, Option] = field(default_factory=dict)
    data: bool = False
    # The option that, given, makes the tag stand alone, enclosing nothing.
    bare_with: str = ""
    public: bool = False
    example: str = ""
    shape: Shape | None = None

    def __post_init__(self) -> None:
        if not self.about:
            raise ValueError(f"The tag {self.name} says what it is.")
        if self.public and not self.example:
            raise ValueError(f"The public tag {self.name} shows itself written.")
        if self.function is not None:
            if self.ordered or self.options or self.data or self.shape:
                raise ValueError(f"The tag {self.name} takes what its function's signature declares.")
            ordered, options = _read_options(self.function)
            object.__setattr__(self, "ordered", ordered)
            object.__setattr__(self, "options", options)
            object.__setattr__(self, "__signature__", inspect.signature(self.function))
            return
        keyword, ordered = inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD

        def parameter(name: str, option: Option, kind: inspect._ParameterKind) -> inspect.Parameter:
            default = inspect.Parameter.empty if option.required else option.default
            return inspect.Parameter(name, kind, default=default)

        # What the template contract records as the tag's parameters.
        signature = inspect.Signature(
            [
                inspect.Parameter("content", ordered),
                *(parameter(name, option, ordered) for name, option in self.ordered.items()),
                *(parameter(name, self.options[name], keyword) for name in sorted(self.options)),
                *([inspect.Parameter("data", inspect.Parameter.VAR_KEYWORD)] if self.data else []),
            ]
        )
        object.__setattr__(self, "__signature__", signature)

    def __call__(self, *args, **kwargs) -> None:
        raise TypeError(f"{self.name} is a template tag, drawn by {self.partial}")

    def admits(self, names: set[str]) -> bool:
        return all(name in self.options or (self.data and name.startswith("data_")) for name in names)

    def refused(self) -> str:
        """Why a page's options are not this tag's, as the template compiles."""

        rest = ", data_*" if self.data else ""
        return f"{self.name} takes {', '.join(sorted(self.options))}{rest} as name=value"

    def missing(self, names: set[str]) -> list[str]:
        """The required options a page did not write."""

        return sorted(name for name, option in self.options.items() if option.required and name not in names)

    def held(self, values: Mapping[str, object]) -> None:
        """Refuse a value given outside its option's choices: the one check
        of a choice, for every part."""

        for name, option in (self.ordered | self.options).items():
            if option.choices and name in values and values[name] not in option.choices:
                said = ", ".join(sorted(map(str, filter(None, option.choices))))
                raise template.TemplateSyntaxError(f"{self.name} {name}= is one of {said}")

    def values(self, given: dict) -> dict:
        """What a page gave, each option it left out at its default and each
        one with choices held to them, as the partial's variables."""

        values = {name: option.default for name, option in (self.ordered | self.options).items()} | given
        self.held(values)
        return self.shape(self.name, values) if self.shape else values


def _drawn_by(part: Part) -> Callable:
    """The function a function tag calls: its own, holding a page's values to
    their choices first when any option has them."""

    function = part.function
    assert function is not None  # noqa: S101 - only a function part reaches here
    if not any(option.choices for option in (part.ordered | part.options).values()):
        return function
    signature = inspect.signature(function)

    @wraps(function)
    def held(*args, **kwargs):
        part.held(signature.bind_partial(*args, **kwargs).arguments)
        return function(*args, **kwargs)

    return held


def slotted(part: Part, *, library: template.Library) -> None:
    """Register the part as its tag in ``library``. A function part is
    Django's simple tag, or with a partial its inclusion tag, taking the
    context when its function's first parameter is ``context``. Any other is
    ``{% name option=value %}first{% second %}...{% endname %}``: markup a
    page would otherwise write by hand, drawn by the part's partial with each
    part as the variable it is named for. The later parts may be left out,
    and come in their declared order."""

    if part.function is not None:
        takes_context = next(iter(inspect.signature(part.function).parameters), "") == "context"
        drawn = _drawn_by(part)
        if part.partial:
            library.inclusion_tag(part.partial, takes_context=takes_context, name=part.name)(drawn)
        else:
            library.simple_tag(drawn, takes_context=takes_context, name=part.name)
        # The template contract reads a tag's declaration from what it wraps.
        library.tags[part.name].__wrapped__ = part  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
        return

    end = f"end{part.name}"

    def compile_(parser, token) -> SlotsNode:
        bits = token.split_contents()
        given = token_kwargs(bits[1:], parser)
        if len(given) != len(bits) - 1 or not part.admits(set(given)):
            raise template.TemplateSyntaxError(part.refused())
        if missing := part.missing(set(given)):
            raise template.TemplateSyntaxError(f"{part.name} is given {'=, '.join(missing)}=")
        found = {name: template.NodeList() for name in part.parts}
        ahead = list(part.parts)
        while True:
            found[ahead.pop(0)] = parser.parse((*ahead, end))
            marker = parser.next_token().contents
            if marker == end:
                return SlotsNode(part, found, given)
            ahead = ahead[ahead.index(marker) :]

    # The template contract reads a tag's declaration from what it wraps.
    compile_.__wrapped__ = part  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
    library.tag(part.name, compile_)
