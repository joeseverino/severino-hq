"""mypy holds a ``Sentence`` to words HQ wrote, as PEP 675 holds a ``LiteralString``.

mypy reads ``LiteralString`` as ``str`` (python/mypy#12554), so this plugin
gives HQ's own ``Sentence`` that meaning. Security property: text a request,
a record or a dependency carried cannot reach a refusal's sentence, or any
parameter annotated ``Sentence``, without mypy reporting it.

An argument for a ``Sentence`` parameter is a sentence when it is a string
literal, a constant bound to one, a value already typed ``Sentence``, or an
f-string, ``+``, ``or``, conditional or case change built from those, where an
f-string may also say a number, a date or a length of time. ``Any`` is not a
sentence, so an unannotated value is refused too. A module that names
``Sentence`` is held to ``return-value`` and ``assignment``, so a function
annotated to return one returns only one.

Plain mypy, as an extension runs it, reads ``Sentence`` as the ``str`` it is.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import partial

from mypy import errorcodes
from mypy.errorcodes import ErrorCode
from mypy.nodes import (
    CallExpr,
    ConditionalExpr,
    Context,
    Expression,
    ListExpr,
    MemberExpr,
    OpExpr,
    RefExpr,
    StrExpr,
    TypeInfo,
)
from mypy.options import Options
from mypy.plugin import (
    AnalyzeTypeContext,
    ClassDefContext,
    FunctionContext,
    FunctionSigContext,
    MethodContext,
    MethodSigContext,
    Plugin,
)
from mypy.plugins.default import DefaultPlugin
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    LiteralType,
    NoneType,
    ProperType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

SENTENCE = "hq.platform.application.record_errors.Sentence"
WRITTEN = "hq.platform.application.record_errors.Written"
REFUSAL_TEXT = ErrorCode("refusal-text", "A Sentence is built only from words HQ wrote", "General")
NOT_WRITTEN = (
    "A Sentence is built from literals, constants, Sentence values, numbers and dates. Text a request or "
    "a record carried is shown as data, in the refusal's details, never in its sentence"
)
UNHELD = "A module that names Sentence is held to return-value and assignment; name it in tools/lint/mypy-reach.toml"
# What an f-string may say beside words: a value HQ writes in its own form.
# A length of time HQ worded (moments.Span) is a sentence of its own.
SPAN = "hq.platform.application.moments.Span"
FACTS = ("builtins.int", "builtins.float", "decimal.Decimal", "datetime.date", "datetime.timedelta", SPAN)
CASE = frozenset({"lower", "capitalize"})
CHOICE_LABEL = re.compile(r"get_\w+_display")
# ``counted`` says a number with the words it is given, which a page's may be
# anyone's: in a sentence, its words are held to be sentences too.
COUNTED = "hq.platform.application.ui.counted"
REFUSAL = "hq.platform.application.record_errors.HostRefusal"
OWN_CONSTRUCTOR = "A refusal is built from its one sentence; it takes HostRefusal's constructor, never its own"
HELD = (errorcodes.RETURN_VALUE, errorcodes.ASSIGNMENT)

type Signature = FunctionSigContext | MethodSigContext
type Call = FunctionContext | MethodContext


class SentencePlugin(Plugin):
    def __init__(self, options: Options) -> None:
        super().__init__(options)
        # mypy asks its own plugin last, so a hook of its for a name runs first, inside this one's.
        self._default = DefaultPlugin(options)
        # The Sentence parameters of each call whose signature was relaxed, read back once it is checked.
        self._pending: dict[int, tuple[Context, list[int]]] = {}

    def get_type_analyze_hook(self, fullname: str) -> Callable[[AnalyzeTypeContext], Type] | None:
        return _written if fullname == SENTENCE else None

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        found = self.lookup_fully_qualified(fullname)
        info = found.node if found else None
        refusal = isinstance(info, TypeInfo) and (info.fullname == REFUSAL or info.has_base(REFUSAL))
        return _one_sentence if refusal else None

    def get_function_signature_hook(self, fullname: str) -> Callable[[FunctionSigContext], CallableType] | None:
        return partial(self._relaxed, self._default.get_function_signature_hook(fullname))

    def get_method_signature_hook(self, fullname: str) -> Callable[[MethodSigContext], CallableType] | None:
        return partial(self._relaxed, self._default.get_method_signature_hook(fullname))

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        return partial(self._judged, self._default.get_function_hook(fullname))

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        return partial(self._judged, self._default.get_method_hook(fullname))

    def _relaxed[C: Signature](self, inner: Callable[[C], CallableType] | None, ctx: C) -> CallableType:
        """The signature with each ``Sentence`` parameter taking a ``str``, which ``_judged`` then reads."""

        signature = inner(ctx) if inner else ctx.default_signature
        marked = [index for index, kind in enumerate(signature.arg_types) if _marked(kind)]
        if not marked:
            return signature
        self._pending[id(ctx.context)] = (ctx.context, marked)
        text = ctx.api.named_generic_type("builtins.str", [])
        return signature.copy_modified(
            arg_types=[text if index in marked else kind for index, kind in enumerate(signature.arg_types)]
        )

    def _judged[C: Call](self, inner: Callable[[C], Type] | None, ctx: C) -> Type:
        context, marked = self._pending.pop(id(ctx.context), (None, []))
        for index in marked if context is ctx.context else ():
            for expression, kind in zip(ctx.args[index], ctx.arg_kinds[index], strict=True):
                if kind.is_star() or not _sentence(expression, ctx):
                    ctx.api.fail(NOT_WRITTEN, expression, code=REFUSAL_TEXT)
        return inner(ctx) if inner else ctx.default_return_type


def _one_sentence(ctx: ClassDefContext) -> None:
    # A constructor of its own would take its sentence through a parameter
    # this plugin never reads, from a call it may never see.
    if "__init__" in ctx.cls.info.names:
        ctx.api.fail(OWN_CONSTRUCTOR, ctx.cls, code=REFUSAL_TEXT)


def _written(ctx: AnalyzeTypeContext) -> Type:
    options = ctx.api.options  # type: ignore[attr-defined]
    if any(code in options.disabled_error_codes for code in HELD):
        ctx.api.fail(UNHELD, ctx.context, code=REFUSAL_TEXT)
    return ctx.api.named_type(WRITTEN, [])


def _marked(kind: Type) -> bool:
    proper = get_proper_type(kind)
    if isinstance(proper, UnionType):
        return any(_marked(item) for item in proper.items)
    return isinstance(proper, Instance) and proper.type.fullname == WRITTEN


def _sentence(expression: Expression, ctx: Call) -> bool:
    match expression:
        case StrExpr():
            return True
        case OpExpr(op="+" | "or"):
            return _sentence(expression.left, ctx) and _sentence(expression.right, ctx)
        case ConditionalExpr():
            return _sentence(expression.if_expr, ctx) and _sentence(expression.else_expr, ctx)
        case CallExpr(callee=MemberExpr(expr=StrExpr(), name="join"), args=[ListExpr(items=items)]):
            # How mypy reads an f-string: its parts joined.
            return all(_sentence(item, ctx) for item in items)
        case CallExpr(callee=MemberExpr(expr=StrExpr(), name="format"), args=[value, *spec]):
            return (_sentence(value, ctx) or _fact(_type(value, ctx))) and all(_sentence(s, ctx) for s in spec)
        case CallExpr(callee=MemberExpr(expr=base, name=name), args=[]) if name in CASE:
            return _sentence(base, ctx)
        case CallExpr(callee=RefExpr(fullname=fullname), args=[count, *words]) if fullname == COUNTED:
            return _fact(_type(count, ctx)) and all(_sentence(word, ctx) for word in words)
        case CallExpr(callee=MemberExpr(expr=base, name=name), args=[]) if CHOICE_LABEL.fullmatch(name):
            # A model's choice label, which HQ declares beside the field.
            return _is_model(_type(base, ctx))
    return _is_written(_type(expression, ctx))


def _type(expression: Expression, ctx: Call) -> ProperType:
    # The checker has typed the arguments by the time a call's hook runs.
    known = ctx.api.lookup_type_or_none(expression)  # type: ignore[attr-defined]
    return get_proper_type(known or AnyType(TypeOfAny.special_form))


def _is_written(kind: ProperType) -> bool:
    match kind:
        case UnionType():
            return all(_is_written(get_proper_type(item)) for item in kind.items)
        case NoneType():
            return True
        case LiteralType():
            return isinstance(kind.value, str)
        case Instance():
            return (
                kind.type.fullname == WRITTEN
                or kind.type.has_base(SPAN)
                or (kind.type.fullname == "builtins.str" and kind.last_known_value is not None)
            )
    return False


def _is_model(kind: ProperType) -> bool:
    return isinstance(kind, Instance) and kind.type.has_base("django.db.models.base.Model")


def _fact(kind: ProperType) -> bool:
    if isinstance(kind, UnionType):
        return all(_fact(get_proper_type(item)) for item in kind.items)
    return isinstance(kind, Instance) and any(kind.type.has_base(base) for base in FACTS)


def plugin(version: str) -> type[Plugin]:
    return SentencePlugin
