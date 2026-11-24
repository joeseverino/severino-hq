"""Findings about a record that points at something that is not there: a
connection no controller has, or a domain HQ neither expects nor reads.

Nothing refuses a reference that matches nothing, since a declaration may name
what HQ only reads (docs/Architecture.md#infrastructure-control-plane). A name is
said to match nothing only when HQ holds the whole list it would be on: some
controller reports its connections, or a reachable read listed the domains.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from hq.domains.control_plane.models import REFERENCE_COLUMNS
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS, read_record

from .connection_lifecycle import UNREPORTED_NODE
from .entity_links import entity_link, node_name
from .finding_model import Finding, FindingEstate, FindingRemedy, FindingRule
from .references import Kind
from .tone import Tone
from .topology_model import TopologyNode
from .ui import counted
lazy from .facts import snapshots_of
lazy from .infrastructure import declared_resources

RULE = "names-nothing"


def containments() -> tuple[tuple[str, str, str, str], ...]:
    """Every "is inside" relation the registry declares, as the kind that
    holds, the kind held, and the field of each that names the holder."""

    return tuple((kind, *provider.contains) for kind, provider in PROVIDERS.items() if provider.contains is not None)


def _reported_connections(nodes: Mapping[str, TopologyNode]) -> frozenset[str]:
    """Every connection a controller has, by its ref.

    A connection node no controller reports stands in for a name that was read
    through once; it is not a connection a controller has.
    """

    return frozenset(
        node.connection_ref
        for node in nodes.values()
        if node.kind == "connection" and node.connection_ref and not node.id.startswith(UNREPORTED_NODE)
    )


def _read_parents(kind: str) -> frozenset[str] | None:
    """The names a reachable read of ``kind`` listed; None when none was reachable."""

    provider = PROVIDERS[kind]
    reachable = False
    names: set[str] = set()
    for snapshot in snapshots_of(kind):
        if not snapshot.reachable:
            continue
        reachable = True
        for record in snapshot.records:
            found = read_record(kind, record, lambda spec: tuple(provider.identity(spec)))
            names.update(normalized_hostname(name) for name in found or ())
    return frozenset(names) if reachable else None


def _missing_parents(resources: Iterable[Any]) -> dict[str, tuple[str, str]]:
    """Each declaration that is inside something HQ neither expects nor has
    read, by key, with the holder's kind and the name that matches nothing."""

    listed = tuple(resources)
    found: dict[str, tuple[str, str]] = {}
    for holder, held, held_field, holder_field in containments():
        if held_field not in REFERENCE_COLUMNS:
            raise ValueError(f"{held}.{held_field} is a reference with no derived column.")
        read = _read_parents(holder)
        if read is None:
            continue
        expected = {
            normalized_hostname((resource.spec or {}).get(holder_field))
            for resource in listed
            if resource.kind == holder
        }
        for resource in listed:
            name = getattr(resource, held_field) if resource.kind == held else ""
            if name and name not in expected and name not in read:
                found[resource.key] = (holder, name)
    return found


def _edit(node: TopologyNode) -> tuple[FindingRemedy, ...]:
    return (
        FindingRemedy(
            capability="infrastructure.resource.update",
            target=node.label,
        ),
    )


def _no_connection(node: TopologyNode, ref: str, reported: tuple[str, ...]) -> Finding:
    name = node_name(node)
    return Finding(
        rule=RULE,
        subject=node.id,
        title=f"{name} uses a connection no controller has",
        severity=Tone.ATTENTION,
        explanation="Nothing reads it or applies its settings.",
        evidence=(
            ("Connection it names", entity_link("connection", ref)),
            ("Connections the controllers have", counted(len(reported), "connection")),
        ),
        remedies=_edit(node),
    )


def _no_parent(node: TopologyNode, kind: str, missing: str) -> Finding:
    name = node_name(node)
    what = (PROVIDERS[kind].label or "record").lower()
    return Finding(
        rule=RULE,
        subject=node.id,
        title=f"{name} is in {missing}, which HQ does not have",
        severity=Tone.ATTENTION,
        explanation="It cannot be applied anywhere.",
        evidence=((f"{what.capitalize()} it names", missing),),
        remedies=_edit(node),
    )


def _names_nothing(estate: FindingEstate) -> tuple[Finding, ...]:
    """A record whose connection or domain matches nothing HQ knows."""

    nodes = {node.id: node for node in estate.nodes()}
    reported = _reported_connections(nodes)
    listed = declared_resources()
    parents = _missing_parents(listed)
    found: list[Finding] = []
    for resource in listed:
        node = nodes.get(Kind.RESOURCE.of(resource.key))
        if node is None or node.kind != "resource" or not node.managed:
            continue
        ref = resource.connection_ref
        if ref and reported and ref not in reported:
            found.append(_no_connection(node, ref, tuple(sorted(reported))))
        kind, missing = parents.get(resource.key, ("", ""))
        if kind in PROVIDERS and missing:
            found.append(_no_parent(node, kind, missing))
    return tuple(sorted(found, key=lambda finding: (finding.title, finding.subject)))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "Missing reference",
        Tone.ATTENTION,
        _names_nothing,
        gathered=True,
        operator_action="Name a connection or domain HQ has, or add the one it names.",
        no_help_reason="HQ cannot tell which one was meant.",
        bars={Tone.ATTENTION: "a record names a connection or domain HQ does not have"},
    ),
)
