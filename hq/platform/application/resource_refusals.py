"""How a resource read is refused, and what each refusal names to get past
it: the field and listing that find a record, the reads of the records or
resources nearest what was asked for, and the resource that holds a type of
record sent as a name."""

import difflib
from collections.abc import Iterable
from typing import Any

from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS

from .integration_specs import ResourceSpec
from .record_errors import HostRefusal, RecordNotFound, Sentence
from .refusal_status import next_call

# The resources that hold each type of managed record (``PROVIDERS``, by its
# ``kind`` filter) and each type of reading (``OBSERVATIONS``, by identifier).
INFRASTRUCTURE = "infrastructure"
READINGS = "readings"


class ResourceError(ValueError):
    """Base for resource failures: ``reason`` is HQ's own text.

    Adapters answer a caller with ``reason``, never ``str(exc)``: a relayed
    handler message can name internals the caller has no business seeing.
    """

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


class UnknownResource(HostRefusal, ResourceError):
    code = "unknown_resource"


class UnsupportedResourceOperation(HostRefusal, ResourceError):
    code = "unsupported_operation"


# What a missed identifier adds to its refusal's sentence.
FOUND_BY = (
    " details.identifier names the field this resource's records are addressed by, and details.next "
    "the listing that finds a record's value."
)
# And what a refusal adds where it names the nearest.
NEAREST_RECORDS = " details.closest reads each record whose identifier is nearest the one asked for."
NEAREST_RESOURCES = " details.closest describes each resource whose name is nearest the one asked for."

# How many of the nearest a refusal offers, each as the call that reads it.
CLOSEST = 3


def closest(asked: str, held: Iterable[str]) -> list[str]:
    """Up to ``CLOSEST`` of ``held`` nearest ``asked``, ignoring case: first
    each that holds it whole (a short name for a full one), shortest first,
    then the most alike by ``difflib``'s ratio."""

    folded = {value.casefold(): value for value in held if value != asked}
    key = asked.casefold()
    holding = sorted((value for value in folded if len(key) >= CLOSEST and key in value), key=len)
    alike = difflib.get_close_matches(key, folded, n=CLOSEST)
    return [folded[value] for value in dict.fromkeys([*holding, *alike])][:CLOSEST]


def found_by(spec: ResourceSpec) -> dict[str, Any]:
    """What finds a record no identifier matched: the field its resource is
    addressed by, and the listing that holds each record's value."""

    details: dict[str, Any] = {"identifier": spec.identifier}
    if spec.list_query_type:
        details["next"] = next_call("list_resource", name=spec.name)
    return details


def missed(refusal: RecordNotFound, spec: ResourceSpec, identifier: object, held: Iterable[object]) -> RecordNotFound:
    """``refusal`` with what finds the record, and the read of each of ``held``,
    the identifiers the caller's listing holds, nearest ``identifier``."""

    refusal.details = found_by(spec)
    values = {str(value): value for value in held}
    if nearest := closest(str(identifier), values):
        refusal.details["closest"] = [
            next_call("get_resource", name=spec.name, identifier=values[value]) for value in nearest
        ]
        refusal.reason += NEAREST_RECORDS
    return refusal


def served(spec: ResourceSpec) -> dict[str, Any]:
    """The read a resource does serve, for a refusal of one it does not: its
    listing, or one record by the field it is addressed by, or else the catalog."""

    if spec.list_handler and spec.list_query_type:
        return {"next": next_call("list_resource", name=spec.name)}
    if spec.detail_handler and spec.identifier:
        return {"identifier": spec.identifier, "next": next_call("get_resource", name=spec.name)}
    return {"next": next_call("describe_resources")}


def unknown(name: str, names: Iterable[str]) -> UnknownResource:
    """The refusal of a name HQ serves no resource by, among ``names``: the
    read of the resource holding a type of record or reading sent as one,
    else the catalog and the resources named nearest it."""

    if name in PROVIDERS:
        return UnknownResource(
            "HQ serves no resource by that name: it is a type of record the infrastructure resource holds, "
            "and details.next lists those records.",
            details={"next": next_call("list_resource", name=INFRASTRUCTURE, filters={"kind": name})},
        )
    if name in OBSERVATIONS:
        return UnknownResource(
            "HQ serves no resource by that name: it is a type of reading the readings resource holds, "
            "and details.next reads it with its records.",
            details={"next": next_call("get_resource", name=READINGS, identifier=name)},
        )
    reason: Sentence = "HQ serves no resource by that name; describe_resources names each one."
    details: dict[str, Any] = {"next": next_call("describe_resources")}
    if nearest := closest(name, names):
        details["closest"] = [next_call("describe_resources", name=value) for value in nearest]
        reason += NEAREST_RESOURCES
    return UnknownResource(reason, details=details)


def unlisted(spec: ResourceSpec) -> UnsupportedResourceOperation:
    return UnsupportedResourceOperation(
        "This resource cannot be listed; details.next is what it does serve.", details=served(spec)
    )


def undetailed(spec: ResourceSpec) -> UnsupportedResourceOperation:
    return UnsupportedResourceOperation(
        "This resource has no detail view; details.next is what it does serve.", details=served(spec)
    )
