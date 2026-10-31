"""Resource tool catalogs derived from the deployment's OpenAPI document."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Resource:
    name: str
    summary: str
    identifier: str = ""


@dataclass(frozen=True, slots=True)
class ResourceContract:
    listable: tuple[Resource, ...]
    addressable: tuple[Resource, ...]


def _parameter(document: dict[str, Any], parameter: dict[str, Any]) -> dict[str, Any]:
    reference = parameter.get("$ref")
    if reference is None:
        return parameter
    prefix = "#/components/parameters/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise ValueError("Resource parameters must use local component references.")
    resolved: dict[str, Any] = document["components"]["parameters"][reference.removeprefix(prefix)]
    if "$ref" in resolved:
        raise ValueError("Resource parameter components must be concrete.")
    return resolved


def resource_contract(document: dict[str, Any]) -> ResourceContract:
    """Read marked GET operations, skipping the generic resource templates."""

    listed: dict[str, Resource] = {}
    detailed: dict[str, Resource] = {}
    for item in document["paths"].values():
        if (marked := _marked_resource(document, item)) is None:
            continue
        resource, detail = marked
        target = detailed if detail else listed
        if resource.name in target:
            raise ValueError(f"Resource {resource.name!r} declares the same operation twice.")
        target[resource.name] = resource
    return ResourceContract(
        tuple(listed[name] for name in sorted(listed)),
        tuple(detailed[name] for name in sorted(detailed)),
    )


def _marked_resource(document: dict[str, Any], item: dict[str, Any]) -> tuple[Resource, bool] | None:
    """The resource a path's GET operation is marked as, and whether it reads one record.

    An operation with no mark is none; one marked without a name, a
    description, or (reading one record) its identifier is refused.
    """

    operation = item.get("get", {})
    name = operation.get("x-hq-resource")
    if name is None:
        return None
    summary = operation.get("description")
    if not _is_text(name) or not _is_text(summary):
        raise ValueError("Resource operations must declare a name and description.")
    detail = _reads_one_record(document, item, operation)
    identifier = operation.get("x-hq-identifier", "")
    if detail and not _is_text(identifier):
        raise ValueError(f"Resource {name!r} must declare its identifier.")
    return Resource(name, summary, identifier if detail else ""), detail


def _reads_one_record(document: dict[str, Any], item: dict[str, Any], operation: dict[str, Any]) -> bool:
    """Whether an operation takes the identifier in its path, at the path's level or its own."""

    parameters = [
        _parameter(document, parameter) for parameter in [*item.get("parameters", []), *operation.get("parameters", [])]
    ]
    return any(p.get("in") == "path" and p.get("name") == "identifier" for p in parameters)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def catalog(resources: tuple[Resource, ...]) -> str:
    """The resource descriptions already emitted by the API contract."""

    return "\n".join(
        f"- `{resource.name}`: {resource.summary}"
        + (f" Identifier: `{resource.identifier}`." if resource.identifier else "")
        for resource in resources
    )
