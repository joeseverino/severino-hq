"""The capability catalog as ``describe_capabilities`` answers an agent: an
index of every capability, then the full entries a name or resource narrows
to.

A narrowed answer carries each schema definition once, at its root, since a
domain's entries share most of theirs.
"""

from typing import Annotated, Any, TypedDict

from hq.platform.application.answer_fields import Pinned, SchemaVersion
from hq.platform.application.capabilities import (
    CapabilityDescription,
    CapabilitySummary,
    capability_index,
    describe_capabilities,
)
from hq.platform.application.describe import Describe
from hq.platform.application.json_schemas import merged_definitions
from hq.platform.application.security import Principal

# The shape describe_capabilities answers an agent in, apart from the API's: it moves when this shape does.
AGENT_CAPABILITIES_SCHEMA_VERSION = 4
AgentCapabilitiesVersion = Annotated[SchemaVersion, Pinned(AGENT_CAPABILITIES_SCHEMA_VERSION)]


class CapabilityIndexEntry(CapabilitySummary):
    """One capability as the index names it, and whether the caller is refused it."""

    refused: Annotated[
        bool, Describe("Whether the caller lacks a permission it requires; absent when the caller may run it.")
    ]


class CapabilityIndex(TypedDict):
    """Every capability the caller's interface can run, each named without its schema."""

    schema_version: AgentCapabilitiesVersion
    capabilities: list[CapabilityIndexEntry]


# A class statement cannot name the key ``$defs``.
CapabilityDetail = TypedDict(
    "CapabilityDetail",
    {
        "schema_version": AgentCapabilitiesVersion,
        "capabilities": list[CapabilityDescription],
        "$defs": Annotated[
            dict[str, dict[str, Any]],
            Describe("Each definition an `input_schema` here refers to, by `#/$defs/<name>` from this answer's root."),
        ],
    },
)
CapabilityDetail.__doc__ = "The capabilities a name or a resource narrows to, in full."


def shared_definitions(entries: list[CapabilityDescription]) -> tuple[list[CapabilityDescription], dict[str, Any]]:
    """``entries`` with each ``input_schema``'s ``$defs`` moved to one map, and that map."""

    definitions: dict[str, Any] = {}
    moved: list[CapabilityDescription] = []
    for entry in entries:
        schema = dict(entry["input_schema"])
        merged_definitions(definitions, schema.pop("$defs", {}))
        moved.append({**entry, "input_schema": schema})
    return moved, definitions


def agent_capabilities(
    *, principal: Principal, name: str = "", resource: str = ""
) -> CapabilityIndex | CapabilityDetail:
    """The index with no narrowing, built from no schema, else the entries ``name`` and ``resource`` narrow to, in full."""

    if name.strip() or resource.strip():
        full, definitions = shared_definitions(
            describe_capabilities(principal=principal, name=name, resource=resource)["capabilities"]
        )
        return {"schema_version": AGENT_CAPABILITIES_SCHEMA_VERSION, "capabilities": full, "$defs": definitions}
    index: list[CapabilityIndexEntry] = [
        {**summary, "refused": not permitted} for summary, permitted in capability_index(principal=principal)
    ]
    return {"schema_version": AGENT_CAPABILITIES_SCHEMA_VERSION, "capabilities": index}
