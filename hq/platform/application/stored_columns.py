"""The rules a model's column holds a value to, laid on the command field that writes it.

A record's model refuses a value in its ``full_clean``, which a save reaches
only after the capability runner has authorized, consented and perhaps held
the call for a person. Each rule the column declares that pydantic has a
constraint for is laid on the command's field of that name as well
(``integration_specs.command_adapter``), so a malformed value is refused as the
command is validated, at every door, and the schema says the rule. The column
stays the one declaration of it.

A ``Rule`` between a row's columns (an end after its start) has no constraint
a schema can state, so its message is said beside the field it is reported
beside, read from the rule itself (``row_rules``).
"""

from collections.abc import Iterable
from dataclasses import MISSING, dataclass, fields, is_dataclass
from decimal import Decimal
from functools import cache, partial
from types import NoneType, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    Required,
    TypeAliasType,
    TypedDict,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from django.core.validators import (
    DecimalValidator,
    MaxLengthValidator,
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
    URLValidator,
)
from django.db.models import CharField, DecimalField, F, FloatField, IntegerField, JSONField, Q, TextField
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import BaseExpression
from pydantic import AfterValidator, Field, GetCoreSchemaHandler, GetJsonSchemaHandler, TypeAdapter
from pydantic_core import CoreSchema, core_schema

from hq.platform.core.rules import Rule

from .command_fields import STORED_IN, Choices, NonEmpty
from .domains import host_records, load
from .json_schemas import json_schema

if TYPE_CHECKING:
    from django.db.models import Model

# The JSON Schema extension that lists, in their own words, the rules between a
# row's columns that a field's value is held to.
ROW_RULES = "x-hq-rules"

# Django's URLValidator, said without the lookaround and the IGNORECASE flag
# a schema's pattern cannot carry: its scheme check, user and password, IPv4,
# bracketed IPv6, a name of letters (any script),
# digits and inner hyphens under a top-level domain or ``localhost``, a port,
# and anything after it that is not a space.
_ANY = r"a-zA-Z\u00a1-\uffff"
# Python's ``\s``, which Django's check means, spelled out: a schema's reader
# (ECMA-262) and pydantic's (Rust) each mean another set by ``\s``.
SPACE = r"\t\n\x0b\x0c\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_LABEL = rf"[{_ANY}0-9](?:[{_ANY}0-9-]{{0,61}}[{_ANY}0-9])?"
_TOP = rf"(?:[{_ANY}][{_ANY}-]{{0,61}}[{_ANY}]|[Xx][Nn]--[a-zA-Z0-9]{{1,59}})"
_OCTET = r"(?:0|25[0-5]|2[0-4][0-9]|1[0-9]?[0-9]?|[1-9][0-9]?)"
IPV4 = rf"{_OCTET}(?:\.{_OCTET}){{3}}"
# RFC 3986's IPv6address in hexadecimal groups, and an IPv4 address mapped
# into one: the forms Django's check and ``ipaddress`` take, within Django's
# 39 characters. Each is unanchored, for a pattern to place.
_H16 = "[0-9a-fA-F]{1,4}"
IPV6 = (
    "(?:"
    + "|".join(
        (
            rf"(?:{_H16}:){{7}}{_H16}",
            *(
                rf"(?:(?:{_H16}:){{0,{before - 1}}}{_H16})?::(?:{_H16}:){{0,{6 - before}}}{_H16}"
                for before in range(1, 7)
            ),
            rf"::(?:{_H16}:){{0,6}}{_H16}",
            rf"(?:(?:{_H16}:){{0,6}}{_H16})?::",
            rf"::(?:[fF]{{4}}:)?{IPV4}",
        )
    )
    + ")"
)
_HOST = (
    rf"(?:{IPV4}|\[{IPV6}\]"
    rf"|{_LABEL}(?:\.{_LABEL})*\.{_TOP}\.?|[Ll][Oo][Cc][Aa][Ll][Hh][Oo][Ss][Tt])"
)


def _anycase(word: str) -> str:
    return "".join(f"[{letter.lower()}{letter.upper()}]" if letter.isalpha() else letter for letter in word)


def url_pattern(schemes: Iterable[str]) -> str:
    """What a URLValidator of ``schemes`` takes, as a schema's pattern."""

    scheme = "|".join(_anycase(name) for name in schemes)
    return (
        rf"^(?:{scheme})://(?:[^{SPACE}:@/\[\]]+(?::[^{SPACE}:@/\[\]]*)?@)?{_HOST}"
        rf"(?::[0-9]{{1,5}})?(?:[/?#][^{SPACE}]*)?$"
    )


def held(command_type: type) -> Any:
    """``command_type`` with each field a model stores held to its column's
    rules, and described with the row's rules between columns, or the type
    itself where none is."""

    markers = _markers(command_type)
    return Annotated[command_type, _Stored(command_type, tuple(markers.items()))] if markers else command_type


def laid_over(command_type: type, *, name: str, doc: str, key: str, key_type: Any) -> Any:
    """A record laid over the one stored under its ``key``: a ``TypedDict``
    of ``command_type``'s fields, each held as the command holds it and
    optional, but ``key``, which it must send holding a value its column
    takes."""

    hints = get_type_hints(command_type, include_extras=True)
    markers = _markers(command_type)
    model = _model_of(command_type)
    column = model._meta.get_field(key) if model is not None else None
    rules = _column_rules(column, empty=False) if column is not None else ()
    keyed = Annotated[key_type, StoredColumn(str, rules)] if rules else key_type
    record: Any = TypedDict(  # type: ignore[misc]  # built from the command's fields
        name,
        {
            item.name: Required[keyed]
            if item.name == key
            else NotRequired[
                Annotated[hints[item.name], *markers[item.name]] if item.name in markers else hints[item.name]
            ]
            for item in fields(command_type)
        },
    )
    record.__doc__ = doc
    return record


def _markers(command_type: type) -> dict[str, tuple[Any, ...]]:
    markers: dict[str, tuple[Any, ...]] = {}
    for name, marker in (*stored_rules(command_type), *row_rules(command_type)):
        markers[name] = (*markers.get(name, ()), marker)
    return markers


def row_rules(command_type: type) -> tuple[tuple[str, Between], ...]:
    """Each field of a command that a model stores, with the messages of the
    model's ``Rule``s reported beside that column which compare it with
    another column."""

    model = _model_of(command_type)
    if model is None or not is_dataclass(command_type):
        return ()
    said: dict[str, list[str]] = {}
    for rule in model._meta.constraints:
        if isinstance(rule, Rule) and rule.field and _columns(rule.condition) - {rule.field}:
            said.setdefault(rule.field, []).append(str(rule.violation_error_message))
    return tuple((item.name, Between(tuple(said[item.name]))) for item in fields(command_type) if item.name in said)


def _columns(node: Any) -> set[str]:
    """The columns a rule's condition names, in its lookups and its expressions."""

    if isinstance(node, Q):
        return set().union(*map(_columns, node.children))
    if isinstance(node, tuple):
        lookup, value = node
        return {lookup.split(LOOKUP_SEP)[0]} | _columns(value)
    if isinstance(node, F):
        return {node.name.split(LOOKUP_SEP)[0]}
    if isinstance(node, BaseExpression):
        return set().union(*map(_columns, node.get_source_expressions()))
    return set()


def stored_rules(command_type: type) -> tuple[tuple[str, Any], ...]:
    """Each field of a command that a model stores, with what the model's
    column of that name holds it to, as a marker pydantic reads.

    ``held`` lays each on its field, so the command refuses what
    the model's ``full_clean`` would before anything else is done with it, at
    every door, and its schema says so, with the pattern of each ``Rule``
    about that column alone. The model is a record domain's, or the
    one a command names with ``stored_in``. A rule pydantic has no constraint
    for (a URL's or an address's validator, a unique column) stays the
    model's alone.
    """

    model = _model_of(command_type)
    if model is None or not is_dataclass(command_type):
        return ()
    columns = {column.name: column for column in model._meta.get_fields() if column.concrete and not column.is_relation}
    hints = get_type_hints(command_type)
    found = []
    for item in fields(command_type):
        column = columns.get(item.name)
        base = _base(column) if column is not None else None
        # A field the command holds in another shape (days it joins into one
        # text) is the save's to convert, so the model's to check.
        if column is None or base is None or not _holds(hints[item.name], base):
            continue
        # An empty value is the model's to fill when the command's default is
        # one (a slug derived from the name), as when the column allows it.
        empty = column.blank or (item.default is not MISSING and item.default in column.empty_values)
        patterns = [
            rule.pattern
            for rule in model._meta.constraints
            if isinstance(rule, Rule) and rule.pattern and rule.field == item.name
        ]
        if rules := (*_column_rules(column, empty=empty), *(Field(pattern=pattern) for pattern in patterns)):
            found.append((item.name, StoredColumn(base, rules)))
    return tuple(found)


@cache
def _model_of(command_type: type) -> type[Model] | None:
    declared = getattr(command_type, STORED_IN, None)
    if declared is not None:
        return declared
    return next((load(records.model) for records in host_records() if load(records.command) is command_type), None)


def _base(column: Any) -> type | None:
    """The value a column holds, which its rules are checked on; ``None`` for
    one whose rules pydantic has no constraint for (a date, a flag)."""

    # A JSON column a command writes holds an object.
    kinds = (
        (CharField, str),
        (TextField, str),
        (IntegerField, int),
        (DecimalField, Decimal),
        (FloatField, float),
        (JSONField, dict),
    )
    return next((base for kind, base in kinds if isinstance(column, kind)), None)


def _bare(hint: Any) -> Any:
    while True:
        if isinstance(hint, TypeAliasType):
            hint = hint.__value__
        elif get_origin(hint) is Annotated:
            hint = get_args(hint)[0]
        else:
            return hint


def _holds(hint: Any, base: type) -> bool:
    """Whether every value a field's type allows, but none, is a ``base``."""

    hint = _bare(hint)
    origin = get_origin(hint)
    if origin is Literal:
        return all(isinstance(value, base) for value in get_args(hint))
    if origin in (Union, UnionType):
        return all(_holds(arm, base) for arm in get_args(hint) if arm is not NoneType)
    held = origin or hint
    return isinstance(held, type) and issubclass(held, base)


def _column_rules(column: Any, *, empty: bool) -> tuple[Any, ...]:
    """What ``column``'s own declaration refuses, as pydantic constraints.

    Django runs no validator on an empty value, so a pattern a column allows
    empty also matches the empty text. A column of choices is held to them,
    which its length limits only restate.
    """

    rules: list[Any] = []
    if column.choices:
        allowed = [(value, str(label)) for value, label in column.flatchoices]
        if empty and all(value != "" for value, _label in allowed):
            allowed.insert(0, ("", "Not set"))
        return (Choices(allowed),)
    if not empty and isinstance(column, (CharField, TextField, JSONField)):
        rules.append(NonEmpty())
    # An integer column's own range is the database's, never a rule a client
    # works within, so only the bounds its declaration names hold.
    bounding = column._validators if isinstance(column, IntegerField) else column.validators
    lows = [rule.limit_value for rule in bounding if isinstance(rule, MinValueValidator)]
    highs = [rule.limit_value for rule in bounding if isinstance(rule, MaxValueValidator)]
    rules += [Field(ge=max(lows))] if lows else []
    rules += [Field(le=min(highs))] if highs else []
    for rule in column.validators:
        if isinstance(rule, MaxLengthValidator):
            rules.append(Field(max_length=rule.limit_value))
        elif isinstance(rule, DecimalValidator):
            rules.append(Field(max_digits=rule.max_digits, decimal_places=rule.decimal_places))
            rules.append(AfterValidator(partial(_to_places, rule.decimal_places)))
        elif isinstance(rule, URLValidator):
            pattern = url_pattern(rule.schemes)
            rules.append(Field(pattern=f"^$|{pattern}" if empty else pattern))
        elif type(rule) is RegexValidator and not rule.inverse_match and not rule.flags:
            # Python's \A and \Z are what the schema's and pydantic's ^ and $ mean.
            pattern = rule.regex.pattern.replace(r"\A", "^").replace(r"\Z", "$")
            rules.append(Field(pattern=f"^$|{pattern}" if empty else pattern))
    if isinstance(column, TextField):
        rules.append(Field(json_schema_extra={"format": "textarea"}))
    return tuple(rules)


def _to_places(places: int, value: Decimal) -> Decimal:
    """``value`` written to the column's places, which pydantic has held it
    within: Django counts the zeros ending a fraction, as pydantic does not."""

    step = Decimal(1).scaleb(-places)
    return value.quantize(step) if value.as_tuple().exponent < -places else value


@dataclass(frozen=True, slots=True)
class StoredColumn:
    """A field held to its model column's rules (``stored_rules``) after its
    own type: checked on the value it validated to, and said in its schema
    beside what the field's type says."""

    base: type
    rules: tuple[Any, ...]

    def _held(self) -> TypeAdapter[Any]:
        return TypeAdapter(Annotated[self.base, *self.rules])

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        held = self._held()

        def check(value: Any) -> Any:
            # A null the field's own type admits is the column's to allow.
            return value if value is None else held.validate_python(value)

        return core_schema.no_info_after_validator_function(check, handler(source))

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        plain = json_schema(TypeAdapter(self.base))
        said = {key: value for key, value in json_schema(self._held()).items() if plain.get(key) != value}
        return {**handler(schema), **said}


@dataclass(frozen=True, slots=True)
class Between:
    """A field whose row is held to rules between its columns, each said in
    the rule's own words under ``ROW_RULES`` beside the field's type, which a
    field of a named type leaves described in that type's definition."""

    said: tuple[str, ...]

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), ROW_RULES: list(self.said)}


@dataclass(frozen=True, slots=True)
class _Stored:
    """A dataclass command whose fields ``columns`` names each take those
    markers after their own type."""

    command_type: type
    columns: tuple[tuple[str, tuple[Any, ...]], ...]

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        schema = handler(source)
        hints = get_type_hints(self.command_type, include_extras=True)
        columns = dict(self.columns)
        for field in handler.resolve_ref_schema(schema)["schema"]["fields"]:
            if (name := field["name"]) in columns:
                # A field with a default holds its type's schema inside the default's.
                holder = field["schema"] if field["schema"]["type"] == "default" else field
                holder["schema"] = handler.generate_schema(Annotated[hints[name], *columns[name]])
        return schema
