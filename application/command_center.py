"""Operator discovery across HQ's declared resources and commands."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from django.urls import reverse

from .capabilities import CapabilitySpec, capability_label, capability_specs
from .connections import (
    ConnectionAbility,
    ConnectionSpec,
    connection_specs,
)
from .contracts import route_url
from .resources import ResourceSpec, resource_specs
from .security import AuthorizationError, Capability, Principal
from .findings import finding_rules
from .topology import topology_lenses


_MATCHING_ABILITY_BADGE_LIMIT = 3


@dataclass(frozen=True)
class DiscoveryItem:
    kind: str
    name: str
    label: str
    summary: str
    url: str
    destination_label: str
    badges: tuple[str, ...]
    search_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandRelation:
    labels: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()


def _permitted(required: tuple[Capability | str, ...], principal: Principal) -> bool:
    try:
        for capability in required:
            principal.require(capability)
    except AuthorizationError:
        return False
    return True


def _matches(item: DiscoveryItem, query: str) -> bool:
    return _contains_all(
        (item.name, item.label, item.summary, *item.badges, *item.search_terms),
        query,
    )


def _contains_all(values: tuple[str, ...], query: str) -> bool:
    terms = query.casefold().split()
    haystack = " ".join(values).casefold()
    return all(term in haystack for term in terms)


def _ability_contains_any(ability: ConnectionAbility, query: str) -> bool:
    haystack = " ".join((ability.name, ability.label, ability.summary)).casefold()
    return any(term in haystack for term in query.casefold().split())


def _connection_matches(spec: ConnectionSpec, query: str) -> bool:
    return _contains_all(
        (
            spec.name,
            spec.label,
            spec.summary,
            *(
                term
                for ability in spec.abilities
                for term in (
                    ability.name,
                    ability.label,
                    ability.summary,
                )
            ),
        ),
        query,
    )


def _matching_ability_labels(spec: ConnectionSpec, query: str) -> tuple[str, ...]:
    """Name why a connection matched instead of showing an opaque family hit."""

    terms = query.casefold().split()
    if not terms:
        return ()
    labels = tuple(
        ability.label
        for ability in spec.abilities
        if _ability_contains_any(ability, query)
    )
    visible = labels[:_MATCHING_ABILITY_BADGE_LIMIT]
    hidden = len(labels) - len(visible)
    if not hidden:
        return visible
    return (*visible, f"+{hidden} matching abilities")


def _command_matches_ability(
    command: CapabilitySpec, ability: ConnectionAbility
) -> bool:
    if ability.capability == command.name:
        return True
    if (
        not ability.subject_resource
        or command.subject_resource != ability.subject_resource
    ):
        return False
    governed = set(ability.governs_kinds)
    constrained_kinds = {
        str(value) for key, value in command.target_query if key == "kind"
    }
    return not constrained_kinds or bool(governed & constrained_kinds)


def _command_item(spec: CapabilitySpec, relation: CommandRelation) -> DiscoveryItem:
    url = reverse("command", kwargs={"name": spec.name})
    if relation.kinds:
        url = f"{url}?{urlencode([('kind', kind) for kind in relation.kinds])}"
    return DiscoveryItem(
        kind="command",
        name=spec.name,
        label=capability_label(spec.name),
        summary=spec.summary,
        url=url,
        destination_label="",
        badges=(
            spec.effect.replace("_", " "),
            *(f"via {label}" for label in relation.labels),
        ),
    )


def _commands_by_resource(
    commands: tuple[CapabilitySpec, ...],
) -> dict[str, tuple[CapabilitySpec, ...]]:
    grouped: dict[str, list[CapabilitySpec]] = {}
    for command in commands:
        if command.subject_resource:
            grouped.setdefault(command.subject_resource, []).append(command)
    return {resource: tuple(items) for resource, items in grouped.items()}


def _candidate_commands(
    ability: ConnectionAbility,
    *,
    by_name: dict[str, CapabilitySpec],
    by_resource: dict[str, tuple[CapabilitySpec, ...]],
) -> tuple[CapabilitySpec, ...]:
    candidates = {
        command.name: command
        for command in by_resource.get(ability.subject_resource, ())
    }
    if explicit := by_name.get(ability.capability):
        candidates.setdefault(explicit.name, explicit)
    return tuple(candidates.values())


def _record_command_relation(
    related_labels: dict[str, dict[str, None]],
    related_kinds: dict[str, dict[str, None]],
    command: CapabilitySpec,
    ability: ConnectionAbility,
) -> None:
    related_labels.setdefault(command.name, {}).setdefault(ability.label, None)
    kinds = related_kinds.setdefault(command.name, {})
    for kind in ability.governs_kinds:
        kinds.setdefault(kind, None)


def _related_command_labels(
    connections: tuple[ConnectionSpec, ...],
    commands: tuple[CapabilitySpec, ...],
    query: str,
) -> dict[str, CommandRelation]:
    by_name = {command.name: command for command in commands}
    by_resource = _commands_by_resource(commands)
    related_labels: dict[str, dict[str, None]] = {}
    related_kinds: dict[str, dict[str, None]] = {}
    for connection in connections:
        if not _connection_matches(connection, query):
            continue
        for ability in connection.abilities:
            if not _ability_contains_any(ability, query):
                continue
            for command in _candidate_commands(
                ability, by_name=by_name, by_resource=by_resource
            ):
                if _command_matches_ability(command, ability):
                    _record_command_relation(
                        related_labels, related_kinds, command, ability
                    )
    return {
        name: CommandRelation(tuple(labels), tuple(related_kinds.get(name, ())))
        for name, labels in related_labels.items()
    }


def _resource_url(spec: ResourceSpec) -> str:
    return route_url(spec.web_route)


def _lens_url(name: str) -> str:
    base = route_url("control_plane:topology")
    return f"{base}?{urlencode({'lens': name})}" if base else ""


def _finding_url(name: str) -> str:
    base = route_url("control_plane:findings")
    return f"{base}?{urlencode({'rule': name})}" if base else ""


def _ability_count(count: int) -> str:
    return f"{count} {'ability' if count == 1 else 'abilities'}"


def command_center(query: str, *, principal: Principal) -> dict:
    """Return every permitted resource and capability matching ``query``."""

    registered_resources = resource_specs()
    registered_commands = capability_specs()
    registered_connections = connection_specs()
    permitted_connections = tuple(
        spec
        for spec in registered_connections
        if _permitted(spec.required_capabilities, principal)
    )
    related_commands = _related_command_labels(
        permitted_connections,
        registered_commands,
        query,
    )
    resources = tuple(
        DiscoveryItem(
            kind="resource",
            name=spec.name,
            label=spec.label,
            summary=spec.summary,
            url=_resource_url(spec),
            destination_label="",
            badges=tuple(
                operation
                for operation, supported in (
                    ("list", spec.list_handler),
                    ("get", spec.detail_handler),
                    ("search", spec.search),
                )
                if supported
            ),
        )
        for spec in registered_resources
        if _permitted(spec.required_capabilities, principal)
    )
    commands = tuple(
        item
        for spec in registered_commands
        if _permitted(spec.required_capabilities, principal)
        for item in (
            _command_item(spec, related_commands.get(spec.name, CommandRelation())),
        )
        if _matches(item, query) or spec.name in related_commands
    )
    connections = tuple(
        DiscoveryItem(
            kind="connection",
            name=spec.name,
            label=spec.label,
            summary=spec.summary,
            url=route_url(spec.web_route),
            destination_label="",
            badges=(
                _ability_count(len(spec.abilities)),
                *((spec.secret_store,) if spec.secret_store else ()),
                *_matching_ability_labels(spec, query),
            ),
            search_terms=tuple(
                term
                for ability in spec.abilities
                for term in (ability.name, ability.label, ability.summary)
            ),
        )
        for spec in permitted_connections
    )
    # A lens is a question about the graph rather than a thing in it, so it
    # costs no query to offer: the declarations are static and the projection is
    # derived only once the operator opens one.
    views = (
        tuple(
            DiscoveryItem(
                kind="view",
                name=lens.name,
                label=lens.label,
                summary=lens.summary,
                url=_lens_url(lens.name),
                destination_label="Topology",
                badges=("topology",),
            )
            for lens in topology_lenses()
        )
        if _permitted((Capability.READ,), principal)
        else ()
    )
    # Rules, not the claims they would make. Live findings here would derive the
    # whole projection on every keystroke.
    checks = (
        tuple(
            DiscoveryItem(
                kind="check",
                name=rule.name,
                label=rule.title,
                summary=rule.severity,
                url=_finding_url(rule.name),
                destination_label="Findings",
                badges=(rule.severity,),
            )
            for rule in finding_rules()
        )
        if _permitted((Capability.READ,), principal)
        else ()
    )
    return {
        "resources": tuple(item for item in resources if _matches(item, query)),
        "commands": commands,
        "connections": tuple(item for item in connections if _matches(item, query)),
        "views": tuple(item for item in views if _matches(item, query)),
        "checks": tuple(item for item in checks if _matches(item, query)),
    }
