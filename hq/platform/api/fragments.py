"""The API document says each described fragment once.

A fragment many declarations share is declared once, where its meaning
lives, as a named type alias (``json_schemas``), and the document writes it
once under that name with each use a ``$ref``. ``described_once`` refuses a
document still holding two identical described fragments, or a use that
describes itself beside a ``$ref`` to a described component. It is a test's
check, run on the host's document and the composed one, never a step of
serving the document: a wording rule fails a gate, never a request.
"""

import json
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

# Values the document carries rather than says: recorded data and schema values.
_VALUES = ("examples", "example", "dataValue", "value", "const", "default", "enum")
# The objects whose keys are names rather than keywords.
_NAME_MAPS = (
    "properties",
    "patternProperties",
    "dependentSchemas",
    "$defs",
    "paths",
    "schemas",
    "parameters",
    "responses",
    "headers",
    "content",
    "securitySchemes",
)


class OpenAPIError(RuntimeError):
    """A route the document cannot describe; fails the build, not the client."""


def pascal(name: str) -> str:
    """``name``'s words run together, each capitalized: ``asset.create`` is ``AssetCreate``."""

    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9A-Za-z]+", name) if part)


def _described(value: Any, *, names: bool = False) -> Iterator[dict[str, Any]]:
    """Every fragment under ``value`` that carries a description."""

    # A map of names (properties, paths, components) holds no keyword, so a
    # property called ``default`` is read like any other.
    if isinstance(value, list):
        for item in value:
            yield from _described(item)
        return
    if not isinstance(value, dict):
        return
    if not names and isinstance(value.get("description"), str):
        yield value
    for key, item in value.items():
        if names or (key not in _VALUES and not key.startswith("x-")):
            yield from _described(item, names=not names and key in _NAME_MAPS)


def _referred(document: dict[str, Any], value: dict[str, Any]) -> str:
    """The component ``value`` names, directly or as its ``schema``, when that
    component describes itself; else ""."""

    schema = value.get("schema")
    ref = value.get("$ref") or (schema.get("$ref") if isinstance(schema, dict) else None)
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return ""
    target: Any = document
    for part in ref.removeprefix("#/").split("/"):
        target = target.get(part) if isinstance(target, dict) else None
    return ref if isinstance(target, dict) and isinstance(target.get("description"), str) else ""


def described_once(document: dict[str, Any]) -> None:
    """Refuse a document holding two identical described fragments, or a
    described use of a described component, naming the first."""

    seen = Counter(json.dumps(fragment, sort_keys=True) for fragment in _described(document))
    repeated = [key for key, count in seen.items() if count > 1]
    if repeated:
        raise OpenAPIError(
            f"{len(repeated)} described fragments are written more than once, first {repeated[0][:200]}. "
            "Declare it once as a named type alias where its meaning lives "
            "(`type Notes = Annotated[str, Describe(...)]`, hq/platform/application/json_schemas.py) "
            "and use the alias in every declaration that repeats it. A parameter or response the builder "
            "writes alike for several operations is a component it names (openapi.py, `_Builder.component`)."
        )
    retold = [ref for fragment in _described(document) if (ref := _referred(document, fragment))]
    if retold:
        raise OpenAPIError(
            f"{len(retold)} uses describe themselves beside the described component they refer to, first one "
            f"of {retold[0]}, so a reader is told its meaning twice. Say it once, in the component's description, "
            "and leave the use a bare reference; a use that means something else is a component of its own."
        )
