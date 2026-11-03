"""The resources the resource tools name, as the resource registry declares them."""

from dataclasses import dataclass

from hq.platform.application.resources import resource_registry


@dataclass(frozen=True, slots=True)
class Resource:
    name: str
    identifier: str = ""


@dataclass(frozen=True, slots=True)
class ResourceContract:
    listable: tuple[Resource, ...]
    addressable: tuple[Resource, ...]
    described: tuple[Resource, ...]


def served_resources() -> ResourceContract:
    """What the resource tools take: each resource the registry serves a listing of, each it
    serves one record of, with the field that record is addressed by, and every one
    ``describe_resources`` describes. The API document serves the same operations from the
    same registry."""

    specs = sorted(resource_registry().values(), key=lambda spec: spec.name)
    return ResourceContract(
        tuple(Resource(spec.name) for spec in specs if spec.list_query_type),
        tuple(Resource(spec.name, spec.identifier) for spec in specs if spec.identifier),
        tuple(Resource(spec.name) for spec in specs),
    )


def identifiers(resources: tuple[Resource, ...]) -> str:
    """The field each resource is addressed by, so a first ``get_resource``
    call names a record right; what each holds is ``describe_resources``'."""

    return ", ".join(f"`{resource.name}` by `{resource.identifier}`" for resource in resources) + "."
