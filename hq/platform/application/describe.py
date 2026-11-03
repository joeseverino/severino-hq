"""What a field is, in a sentence, carried in its ``Annotated`` type.

A leaf of its own, importing nothing of HQ, so a contract that must stay a
leaf (``workflow_contracts``) describes its fields too. The other markers live
in ``command_fields``, which re-exports this one.

A description is the field's meaning, said once in the words a page shows,
since a command's form shows it as the field's help. What the schema's
keywords already say stays out of it: a closed set is the field's type (a
``Literal`` from the constant the model's choices come from, so the schema
carries its ``enum``), and a date's format is its ``format``. What only a
client reading JSON needs (what null or empty means, a ``type:key`` shape,
that the value is a slug or an id) is the marker's second part, ``api``,
which the schema's description adds after the meaning and a form leaves off
(``meanings``). ``lint:prose`` holds both, as every string under ``hq/``
that can reach a page.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Annotated, Any, TypeAliasType, Union, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic_core import CoreSchema


@dataclass(frozen=True, slots=True)
class Describe:
    """What a field is, in a sentence a person reads beside it, and what a
    client reading JSON needs besides:
    ``ended_on: Annotated[date | None, Describe("When it ended.", api="Null while it runs.")]``."""

    text: str
    api: str = ""

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        return {**handler(schema), "description": f"{self.text} {self.api}" if self.api else self.text}


@cache
def meanings(kind: type) -> dict[str, str]:
    """Each field of ``kind`` its ``Describe`` says the meaning of, by name: the
    help a form shows, without the part only a client reads."""

    found = {name: _described(hint) for name, hint in get_type_hints(kind, include_extras=True).items()}
    return {name: said.text for name, said in found.items() if said is not None}


def _described(hint: Any) -> Describe | None:
    # The marker nearest the field wins, as it does in the schema: a use's
    # own over its alias's, and the last of one ``Annotated`` over the rest.
    if isinstance(hint, TypeAliasType):
        return _described(hint.__value__)
    if get_origin(hint) is Annotated:
        inner, *markers = get_args(hint)
        said = [marker for marker in markers if isinstance(marker, Describe)]
        return said[-1] if said else _described(inner)
    if get_origin(hint) is Union:
        return next((said for arg in get_args(hint) if (said := _described(arg)) is not None), None)
    return None


def listed(values: Iterable[str]) -> str:
    """A closed set's values as a description names them: ``a``, ``b`` or ``c``,
    leaving out an empty one."""

    named = [f"`{value}`" for value in values if value]
    return f"{', '.join(named[:-1])} or {named[-1]}" if len(named) > 1 else "".join(named)
