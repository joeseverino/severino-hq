"""What a field is, in a sentence, carried in its ``Annotated`` type.

A leaf importing nothing of HQ, so a leaf contract (``workflow_contracts``)
describes its fields too. The meaning is in a page's words, since a form shows
it as help; what only a JSON client needs is ``api``, which a form leaves off.
What the schema's keywords already say stays out.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Annotated, Any, TypeAliasType, Union, get_args, get_origin, get_type_hints

lazy from pydantic.json_schema import Examples

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
    examples: tuple[Any, ...] = ()

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        said = (
            Examples(list(self.examples)).__get_pydantic_json_schema__(schema, handler)
            if self.examples
            else handler(schema)
        )
        return {**said, "description": " ".join(part for part in (self.text, self.api) if part)}


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
