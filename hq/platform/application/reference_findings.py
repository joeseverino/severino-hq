"""Findings about a record that points at something that is not there: a
connection no controller has, or a domain HQ neither expects nor reads.

A declaration points at another thing by a name in its ``spec``, and the
database derives that name into a column (``ManagedResource.zone``,
``ManagedResource.connection_ref``). Nothing refuses a name that matches
nothing, because a declaration may name what HQ only reads. So the link is
checked here, against everything HQ expects and everything it has read, and a
name that matches neither is one fact on the record's node.

A name is only said to match nothing when HQ holds the whole list it would be
on: some controller reports its connections, or a reachable read listed the
domains. Without that list HQ does not know, and says nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from hq.domains.control_plane.models import REFERENCE_COLUMNS
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS

from .entity_links import node_name
from .finding_model import Finding, FindingEstate, FindingRule, Remedy, fact_values
from .topology_model import TopologyNode
from .ui import counted

RULE = "names-nothing"
NO_CONNECTION = "names-no-connection"
# The kind of thing that is missing, beside its name, as ``kind\x1fname``.
NO_PARENT = "names-no-parent"
_SEPARATOR = "\x1f"

# A connection node no controller reports stands in for a name that was read
# through once; it is not a connection a controller has.
_UNREPORTED = "connection:unreported:"


def containments() -> tuple[tuple[str, str, str, str], ...]:
    """Every "is inside" relation the registry declares, as the kind that
    holds, the kind held, and the field of each that names the holder."""

    return tuple(
        (kind, *provider.contains)
        for kind, provider in PROVIDERS.items()
        if provider.contains is not None
    )


def _reported_connections(nodes: Mapping[str, TopologyNode]) -> frozenset[str]:
    """Every connection a controller has, by its ref."""

    return frozenset(
        node.connection_ref
        for node in nodes.values()
        if node.kind == "connection"
        and node.connection_ref
        and not node.id.startswith(_UNREPORTED)
    )


def _read_parents(kind: str) -> frozenset[str] | None:
    """The names a reachable read of ``kind`` listed; None when none was reachable."""

    from .facts import snapshots_of

    provider = PROVIDERS[kind]
    reachable = False
    names: set[str] = set()
    for snapshot in snapshots_of(kind):
        if not snapshot.reachable:
            continue
        reachable = True
        for record in snapshot.records:
            try:
                found = provider.identity(provider.from_record(record))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            names.update(normalized_hostname(name) for name in found)
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


def add(nodes: dict[str, TopologyNode], resources: Iterable[Any]) -> None:
    """One fact on each declaration's node per link that names nothing."""

    listed = tuple(resources)
    reported = _reported_connections(nodes)
    parents = _missing_parents(listed)
    for resource in listed:
        node = nodes.get(f"resource:{resource.key}")
        if node is None:
            continue
        facts: list[tuple[str, str]] = []
        ref = resource.connection_ref
        if ref and reported and ref not in reported:
            facts.append((NO_CONNECTION, ref))
        if resource.key in parents:
            facts.append((NO_PARENT, _SEPARATOR.join(parents[resource.key])))
        if facts:
            nodes[node.id] = replace(node, facts=node.facts + tuple(facts))


def _edit(node: TopologyNode) -> tuple[Remedy, ...]:
    return (
        Remedy(
            capability="infrastructure.resource.update",
            target=node.label,
            label="Change what HQ expects",
            effect="",
        ),
    )


def _no_connection(node: TopologyNode, ref: str, reported: tuple[str, ...]) -> Finding:
    name = node_name(node)
    return Finding(
        rule=RULE,
        subject=node.id,
        title=f"{name} uses a connection no controller has",
        severity="attention",
        explanation=(
            f"It names the connection {ref}. No controller has one called that, so "
            f"nothing reads {name} and nothing applies HQ's settings to it."
        ),
        evidence=(
            ("Connection it names", ref),
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
        severity="attention",
        explanation=(
            f"HQ expects no {what} called {missing} and no connection reads one, so "
            f"{name} cannot be applied anywhere."
        ),
        evidence=((f"{what.capitalize()} it names", missing),),
        remedies=_edit(node),
    )


def _names_nothing(estate: FindingEstate) -> tuple[Finding, ...]:
    """A record whose connection or domain matches nothing HQ knows.

    The facts are added while the estate is built (``add``), where the
    declarations and the readings are already in hand; a rule reads the
    estate and asks nothing of its own.
    """

    reported = tuple(sorted(_reported_connections({node.id: node for node in estate.nodes()})))
    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "resource" or not node.managed:
            continue
        for ref in fact_values(node, NO_CONNECTION):
            found.append(_no_connection(node, ref, reported))
        for fact in fact_values(node, NO_PARENT):
            kind, _, missing = fact.partition(_SEPARATOR)
            if kind in PROVIDERS and missing:
                found.append(_no_parent(node, kind, missing))
    return tuple(sorted(found, key=lambda finding: (finding.title, finding.subject)))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "A record names something that is not there",
        "attention",
        _names_nothing,
        operator_action=(
            "Change the record to name a connection or a domain HQ has, or add the "
            "one it names."
        ),
        no_help_reason="HQ cannot tell which one was meant.",
    ),
)
