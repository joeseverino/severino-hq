"""The capability catalog as ``describe_capabilities`` answers an agent, in two
levels as an API catalog is read: an index of every capability, then the
full entries of those a name or a resource narrows to.

A narrowed answer carries each schema definition once, at its root, where
every ``input_schema``'s ``#/$defs/<name>`` reads it from, since the entries
of one domain share most of theirs. The machine API answers every entry in
full, each schema whole (``api.views.capabilities``).
"""

from typing import Annotated, Any, TypedDict

from hq.platform.application.answer_fields import Pinned, SchemaVersion
from hq.platform.application.capabilities import (
    CapabilityDescription,
    CapabilitySummary,
    describe_capabilities,
)
from hq.platform.application.describe import Describe
from hq.platform.application.security import Principal

# The shape describe_capabilities answers an agent in, apart from the API's: it moves when this shape does.
AGENT_CAPABILITIES_SCHEMA_VERSION = 3
AgentCapabilitiesVersion = Annotated[SchemaVersion, Pinned(AGENT_CAPABILITIES_SCHEMA_VERSION)]


class CapabilityIndex(TypedDict):
    """Every capability the caller's interface can run, each named without its schema."""

    schema_version: AgentCapabilitiesVersion
    capabilities: list[CapabilitySummary]


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


class ClashingDefinition(ValueError):
    """Two commands define one name differently, so an answer cannot carry both under it."""


def shared_definitions(entries: list[CapabilityDescription]) -> tuple[list[CapabilityDescription], dict[str, Any]]:
    """``entries`` with each ``input_schema``'s ``$defs`` moved to one map, and that map.

    A definition is named for the type it is, as the API document's
    components are, so one name holds one meaning across every command.
    """

    definitions: dict[str, Any] = {}
    moved: list[CapabilityDescription] = []
    for entry in entries:
        schema = dict(entry["input_schema"])
        for name, definition in schema.pop("$defs", {}).items():
            if definitions.setdefault(name, definition) != definition:
                raise ClashingDefinition(
                    f"Two commands define {name!r} differently: each meaning is a type alias of its own name."
                )
        moved.append({**entry, "input_schema": schema})
    return moved, definitions


def agent_capabilities(
    *, principal: Principal, name: str = "", resource: str = ""
) -> CapabilityIndex | CapabilityDetail:
    """The index with no narrowing, else the entries ``name`` and ``resource`` narrow to, in full."""

    entries = describe_capabilities(principal=principal, name=name, resource=resource)["capabilities"]
    if not (name.strip() or resource.strip()):
        index: list[CapabilitySummary] = [
            {
                "name": entry["name"],
                "label": entry["label"],
                "summary": entry["summary"],
                "effect": entry["effect"],
                "resource": entry["resource"],
                "permitted": entry["permitted"],
            }
            for entry in entries
        ]
        return {"schema_version": AGENT_CAPABILITIES_SCHEMA_VERSION, "capabilities": index}
    full, definitions = shared_definitions(entries)
    return {"schema_version": AGENT_CAPABILITIES_SCHEMA_VERSION, "capabilities": full, "$defs": definitions}
