"""Resource tool catalogs derived from the deployment's OpenAPI document."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Resource:
    name: str
    summary: str
    identifier: str = ""


@dataclass(frozen=True)
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
        operation = item.get("get", {})
        name = operation.get("x-hq-resource")
        if name is None:
            continue
        summary = operation.get("description")
        if not isinstance(name, str) or not name or not isinstance(summary, str) or not summary:
            raise ValueError("Resource operations must declare a name and description.")
        parameters = [
            _parameter(document, parameter)
            for parameter in [*item.get("parameters", []), *operation.get("parameters", [])]
        ]
        detail = any(p.get("in") == "path" and p.get("name") == "identifier" for p in parameters)
        identifier = operation.get("x-hq-identifier", "")
        if detail and (not isinstance(identifier, str) or not identifier):
            raise ValueError(f"Resource {name!r} must declare its identifier.")
        resource = Resource(name, summary, identifier if detail else "")
        target = detailed if detail else listed
        if name in target:
            raise ValueError(f"Resource {name!r} declares the same operation twice.")
        target[name] = resource
    return ResourceContract(
        tuple(listed[name] for name in sorted(listed)),
        tuple(detailed[name] for name in sorted(detailed)),
    )


def catalogue(resources: tuple[Resource, ...]) -> str:
    """The resource descriptions already emitted by the API contract."""

    return "\n".join(
        f"- `{resource.name}`: {resource.summary}"
        + (f" Identifier: `{resource.identifier}`." if resource.identifier else "")
        for resource in resources
    )
