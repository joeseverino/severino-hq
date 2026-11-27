"""A value a template writes as a literal is read as the template compiles.

A filter's or a tag's parameter declares how what a page writes is read,
once, on its annotation: a ``Reads`` function, or the values a ``Literal`` or
an enum names. ``reading`` reads each argument through it on every call, and
the engine reads each literal through the same function as it compiles a
template (``held``, from template_engine's loaders, and the compile function
of each ``Part``), so a phrase, a form or a choice a page writes wrong is
refused before any page draws it. A value an include hands over by name
(``{% include "x.html" with name="lock" %}``) is read where the included
template hands it to a filter.
"""

import functools
import inspect
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from django.template import TemplateSyntaxError
from django.template.base import FilterExpression, Node, NodeList, Variable
from django.template.defaulttags import ForNode, WithNode
from django.template.loader_tags import IncludeNode
from django.template.smartif import TokenBase

type Reader = Callable[[Any], Any]

# What a literal is, read where Django leaves a word for render time.
_WORDS = {"True": True, "False": False, "None": None}
# The templates being read now, so an include of itself reads once.
_reading: ContextVar[frozenset[str]] = ContextVar("reading", default=frozenset())


@dataclass(frozen=True)
class Reads:
    """An annotation's marker: the function that reads what a page writes for
    the parameter, answering what the function takes and raising ValueError
    (or TemplateSyntaxError) on what it refuses."""

    read: Reader


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


def one_of(choices: tuple[object, ...]) -> Reader:
    """The reader of a value that is one of ``choices``."""

    def read(value: object) -> object:
        if value not in choices:
            said = ", ".join(sorted(map(str, filter(None, choices))))
            raise ValueError(f"is one of {said}; got {value!r}")
        return value

    return read


def reader_of(annotation: Any) -> Reader | None:
    """How a parameter typed ``annotation`` reads what it is given: its
    ``Reads``, else the choices its type names, else as it comes (None)."""

    if get_origin(annotation) is Annotated:
        marked = [marker.read for marker in get_args(annotation)[1:] if isinstance(marker, Reads)]
        if marked:
            return marked[0]
    choices = choices_of(annotation)
    return one_of(choices) if choices else None


@functools.cache
def readers(function: Callable) -> Mapping[str, tuple[int | None, Reader]]:
    """Each parameter of ``function`` that reads what it is given: its
    position (None for one given only by name) and its reader. Read at first
    use, once every name its annotations use is defined."""

    hints = get_type_hints(function, include_extras=True)
    found = {}
    for index, parameter in enumerate(inspect.signature(function).parameters.values()):
        reader = reader_of(hints.get(parameter.name))
        if reader is None or parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        found[parameter.name] = (None if parameter.kind is parameter.KEYWORD_ONLY else index, reader)
    return found


def read(reader: Reader, value: Any, name: str) -> Any:
    """``value`` read by ``reader``, a refusal said as the template's error,
    after ``name``, what the template wrote it as."""

    try:
        return reader(value)
    except (ValueError, TemplateSyntaxError) as error:
        raise TemplateSyntaxError(f"{name} {error}") from None


def reading(function: Callable) -> Callable:
    """``function``, each argument a parameter reads (``readers``) read before
    it is called: the one place a filter's or a tag's argument is read."""

    @functools.wraps(function)
    def called(*args: Any, **kwargs: Any) -> Any:
        given = list(args)
        for name, (index, reader) in readers(function).items():
            if index is not None and index < len(given):
                given[index] = read(reader, given[index], f"{function.__name__} {name}=")
            elif name in kwargs:
                kwargs[name] = read(reader, kwargs[name], f"{function.__name__} {name}=")
        return function(*given, **kwargs)

    return called


def literal(value: Any, bound: Mapping[str, Any] | None = None) -> tuple[bool, Any]:
    """``(True, the value)`` when ``value``, an expression or a variable of a
    compiled template, is written as a literal or names one ``bound`` holds;
    else ``(False, None)``."""

    if isinstance(value, FilterExpression):
        if value.filters:
            return False, None
        value = value.var
    if not isinstance(value, Variable):
        return True, value
    if value.literal is not None:
        return True, value.literal
    lookups = value.lookups or ()
    if len(lookups) == 1 and lookups[0] in _WORDS:
        return True, _WORDS[lookups[0]]
    if bound and len(lookups) == 1 and lookups[0] in bound:
        return True, bound[lookups[0]]
    return False, None


def _expressions(value: Any, line: int, bound: Mapping[str, Any], seen: set[int]) -> Iterator[tuple[int, Any, Any]]:
    """Each filter expression under ``value``, with its line, what is bound
    there by name, and the node it stands in."""

    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, FilterExpression):
        yield line, value, bound
    elif isinstance(value, Node):
        line = getattr(getattr(value, "token", None), "lineno", None) or line
        if isinstance(value, ForNode):
            bound = {name: given for name, given in bound.items() if name not in value.loopvars}
        elif isinstance(value, WithNode):
            bound = {name: given for name, given in bound.items() if name not in value.extra_context}
        elif isinstance(value, IncludeNode):
            yield line, value, bound
        for attribute in vars(value).values():
            yield from _expressions(attribute, line, bound, seen)
    elif isinstance(value, TokenBase):
        for attribute in vars(value).values():
            yield from _expressions(attribute, line, bound, seen)
    elif isinstance(value, (NodeList, list, tuple)):
        for item in value:
            yield from _expressions(item, line, bound, seen)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _expressions(item, line, bound, seen)


@dataclass(frozen=True)
class _Walk:
    """One template's read: the engine that loads what it includes, and, in a
    template another includes, the literals that include hands over by name,
    which are all it reads there (the template reads its own as it loads)."""

    engine: Any
    included: bool = False

    def written(self, value: Any, bound: Mapping[str, Any]) -> tuple[bool, Any]:
        given, said = literal(value, bound)
        return (given and not literal(value)[0], said) if self.included else (given, said)

    def filtered(self, expression: FilterExpression, bound: Mapping[str, Any]) -> Iterator[str]:
        """Each refusal of a literal ``expression`` hands a filter."""

        given, value = self.written(expression.var, bound)
        for function, arguments in expression.filters:
            found = dict(readers(inspect.unwrap(function)).values())
            reads = [(0, given, value)] + [
                (at, *self.written(argument, bound)) if lookup else (at, not self.included, argument)
                for at, (lookup, argument) in enumerate(arguments, 1)
            ]
            for at, read_here, said in reads:
                if read_here and at in found:
                    try:
                        found[at](said)
                    except (ValueError, TemplateSyntaxError) as error:
                        yield f"|{getattr(function, '_filter_name', function.__name__)}: {error}"
            given = False

    def include(self, node: IncludeNode, bound: Mapping[str, Any]) -> Iterator[str]:
        """Each refusal of a literal the include hands over by name."""

        written, name = literal(node.template, bound)
        if not written or not isinstance(name, str) or name.partition("#")[0] in _reading.get():
            return
        handed = {}
        for key, value in node.extra_context.items():
            given, said = self.written(value, bound)
            if given:
                handed[key] = said
        if handed:
            inner = _Walk(self.engine, included=True)
            yield from (said for _line, said in inner.refusals(self.engine.get_template(name).nodelist, handed))

    def refusals(self, nodelist: NodeList, bound: Mapping[str, Any]) -> list[tuple[int, str]]:
        """Each literal under ``nodelist`` a filter refuses, by line."""

        found = []
        for line, value, there in _expressions(nodelist, 1, bound, set()):
            reads = self.include(value, there) if isinstance(value, IncludeNode) else self.filtered(value, there)
            found += [(line, said) for said in reads]
        return found


def held(template: Any) -> Any:
    """``template``, compiled, once no literal it writes is refused."""

    name = template.origin.template_name or template.origin.name
    with _reading.set(_reading.get() | {name}):
        found = _Walk(template.engine).refusals(template.nodelist, {})
    if found:
        raise TemplateSyntaxError("; ".join(f"{name}:{line}: {said}" for line, said in found))
    return template
