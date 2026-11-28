"""What the API document's builder shares with its parts: its build failure,
its component names, and the reading of the whole that refuses a document
saying a thing twice, or a part of it not at all."""

import json
import logging
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

from django.conf import settings
from django.utils.text import capfirst

logger = logging.getLogger("severino.api")


class OpenAPIError(RuntimeError):
    """A route the document cannot describe; fails the build, not the client."""


def pascal(name: str) -> str:
    """``name``'s words run together, each capitalized: ``asset.create`` is ``AssetCreate``."""

    return "".join(capfirst(part) for part in re.split(r"[^0-9A-Za-z]+", name) if part)


def camel(name: str) -> str:
    """``name`` as ``pascal`` writes it, its first letter lowercased: ``asset.create`` is ``assetCreate``."""

    said = pascal(name)
    # An identifier, camel-cased, is no label.
    # ast-grep-ignore: first-letter-lowered-by-lower-first
    return said[:1].lower() + said[1:]


# Values the document carries rather than says.
_VALUES = frozenset({"examples", "example", "const", "default", "enum"})
# The objects whose keys are names rather than keywords, so a property called
# ``default`` is read like any other.
_NAME_MAPS = frozenset(
    {
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
    }
)
# The one object written bare by design: every list's own route narrows the
# base's items to its row type through ``allOf``.
_NARROWED = "components/schemas/ResourceCollection/properties/items/items"


def _parts(value: Any, path: str = "", *, names: bool = False) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every object of the document that is a part of it, with its path."""

    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _parts(item, f"{path}/{index}")
        return
    if not isinstance(value, dict):
        return
    if not names:
        yield path, value
    for key, item in value.items():
        if names or (key not in _VALUES and not key.startswith("x-")):
            yield from _parts(item, f"{path}/{key}".lstrip("/"), names=not names and key in _NAME_MAPS)


def _at(document: dict[str, Any], ref: Any) -> Any:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    target: Any = document
    for part in ref.removeprefix("#/").split("/"):
        target = target.get(part) if isinstance(target, dict) else None
    return target


def _says(document: dict[str, Any], schema: Any, seen: frozenset[str] = frozenset()) -> bool:
    """Whether ``schema`` says what it holds: by its own description, by what
    it refers to, or by every member of its union but null."""

    if not isinstance(schema, dict):
        return False
    if isinstance(schema.get("description"), str):
        return True
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in seen:
        return _says(document, _at(document, ref), seen | {ref})
    members = [part for key in ("anyOf", "oneOf") for part in schema.get(key, ()) if part.get("type") != "null"]
    return bool(members) and all(_says(document, part, seen) for part in members)


def _refused(document: dict[str, Any]) -> Iterator[str]:
    """Why the document says a thing twice, or a part of it not at all."""

    # A response that leads somewhere is written out for its operation, its
    # links beside the shared schema (``openapi._Builder._responses``).
    described = [
        (path, part)
        for path, part in _parts(document)
        if isinstance(part.get("description"), str) and "links" not in part
    ]
    written = [json.dumps(part, sort_keys=True) for _, part in described]
    seen = Counter(written)
    if repeated := next((path for (path, _), said in zip(described, written, strict=True) if seen[said] > 1), None):
        yield (
            f"{repeated} is a described fragment written more than once. Declare it once as a named type alias "
            "where its meaning lives (`type Notes = Annotated[str, Describe(...)]`) and use the alias in every "
            "declaration that repeats it; a parameter or response the builder writes alike for several "
            "operations is a component it names (`_Builder.component`)."
        )
    for path, part in described:
        schema = part.get("schema")
        ref = part.get("$ref") or (schema.get("$ref") if isinstance(schema, dict) else None)
        target = _at(document, ref)
        if isinstance(target, dict) and isinstance(target.get("description"), str):
            yield f"{path} describes itself beside {ref}, which says it already: leave the use a bare reference."
    for name, schema in document["components"]["schemas"].items():
        for field, value in (schema.get("properties") or {}).items():
            if not _says(document, value):
                yield (
                    f"{name}.{field} says nothing of what it holds: `Annotated[T, Describe(...)]` where it is "
                    "declared, or the docstring of the type it refers to."
                )
    for path, part in _parts(document):
        if part == {"type": "object"} and path != _NARROWED:
            yield (
                f"{path} is an object naming none of its fields: declare a TypedDict, each field with "
                "`Describe`, or use `AsRead` for a part in its source's own shape."
            )
    for section in ("parameters", "headers"):
        for name, parameter in document["components"].get(section, {}).items():
            if not _says(document, parameter) and not _says(document, parameter.get("schema")):
                yield f"The {section[:-1]} {name} says nothing of what it is."
    for path, item in document["paths"].items():
        for method, operation in item.items():
            for parameter in operation.get("parameters", ()):
                if "$ref" not in parameter and not (
                    _says(document, parameter) or _says(document, parameter.get("schema"))
                ):
                    yield f"{method.upper()} {path} {parameter['name']} says nothing of what it is."
    for tag in document["tags"]:
        if not tag.get("description", "").strip():
            yield f"The tag {tag['name']} says nothing of what it holds."


def said_once(document: dict[str, Any]) -> dict[str, Any]:
    """``document``, refused where a developer sees it when it says a thing
    twice or a part of it not at all. Served, it is never refused: a part
    derived from what an operator holds (a playbook's inputs) has no
    declaration in HQ to fix, so what it says wrong is logged."""

    if problems := list(_refused(document)):
        said = f"{len(problems)} parts of the API document are said wrong, first: {problems[0]}"
        if settings.SEVERINO_STRICT_PRINT:
            raise OpenAPIError(said)
        logger.error(said, extra={"event": "api.document.refused"})
    return document
