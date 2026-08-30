"""Operator discovery across HQ's declared resources and commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from django.urls import reverse

from .capabilities import CapabilitySpec, capability_label, capability_specs
from .connections import (
    ConnectionAbility,
    ConnectionSpec,
    connection_catalog,
    connection_specs,
)
from .contracts import route_url
from .resources import ResourceSpec, resource_specs
from .security import Capability, Principal
from .findings import finding_rules
from .topology import topology_lenses


_MATCHING_ABILITY_BADGE_LIMIT = 3
_SEARCH_WORD = re.compile(r"[a-z0-9]+")


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
    return principal.permits(*required)


def _matches(item: DiscoveryItem, query: str) -> bool:
    return _contains_all(
        (item.name, item.label, item.summary, *item.badges, *item.search_terms),
        query,
    )


def _contains_all(values: tuple[str, ...], query: str) -> bool:
    terms = _SEARCH_WORD.findall(query.casefold())
    words = _SEARCH_WORD.findall(" ".join(values).casefold())
    # Two-letter input is a person beginning a word, not permission to match
    # the same two characters in the middle of everything. `sp` should find
    # splits and spending; it should never find infrastructure.
    return all(
        any(word.startswith(term) if len(term) <= 2 else term in word for word in words)
        for term in terms
    )


def _term_score(value: str, term: str) -> int:
    words = _SEARCH_WORD.findall(value.casefold())
    if term in words:
        return 12
    if any(word.startswith(term) for word in words):
        return 8
    if len(term) > 2 and any(term in word for word in words):
        return 3
    return 0


def _match_score(item: DiscoveryItem, query: str) -> int:
    """Prefer what an operator named over incidental supporting evidence."""

    terms = _SEARCH_WORD.findall(query.casefold())
    # A controller-qualified connection id is globally unique, but its prefix
    # is context rather than the connection's own name. Searching `homelab`
    # should rank `homelab-npm` above every unrelated credential observed by a
    # controller named `homelab-server`.
    primary_name = (
        item.name.rpartition(":")[2]
        if item.kind == "connection" and ":" in item.name
        else item.name
    )
    return sum(
        max(
            *(_term_score(value, term) * 4 for value in (item.label, primary_name)),
            *(_term_score(value, term) * 2 for value in (item.summary, *item.badges)),
            *(_term_score(value, term) for value in item.search_terms),
        )
        for term in terms
    )


def _matching(items: tuple[DiscoveryItem, ...], query: str) -> tuple[DiscoveryItem, ...]:
    matched = tuple(item for item in items if _matches(item, query))
    if not query:
        return matched
    return tuple(sorted(matched, key=lambda item: _match_score(item, query), reverse=True))


def _ability_contains_any(ability: ConnectionAbility, query: str) -> bool:
    return any(
        _contains_all((ability.name, ability.label, ability.summary), term)
        for term in _SEARCH_WORD.findall(query.casefold())
    )


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


def _live_connection_url(group, connection) -> str:
    relationship = next(
        (action.url for action in connection.actions if action.name == "relationships"),
        "",
    )
    return relationship or route_url(group.spec.web_route)


def _live_connection_item(group, connection) -> DiscoveryItem:
    instance = connection.instance
    abilities = tuple(state.ability for state in connection.abilities)
    return DiscoveryItem(
        kind="connection",
        name=instance.id,
        label=instance.label,
        summary=instance.detail or instance.endpoint or group.spec.summary,
        url=_live_connection_url(group, connection),
        destination_label=group.spec.label,
        badges=(instance.status_label, _ability_count(len(abilities))),
        search_terms=(
            group.spec.label,
            group.spec.summary,
            instance.kind,
            instance.endpoint,
            instance.controller_id,
            *(fact.label for fact in instance.facts),
            *(fact.value for fact in instance.facts),
            *(target.label for target in instance.targets),
            *(dependency.label for dependency in instance.dependencies),
            *(ability.name for ability in abilities),
            *(ability.label for ability in abilities),
            *(ability.summary for ability in abilities),
            *(scope for state in connection.abilities for scope in state.missing_scopes),
        ),
    )


def command_center(
    query: str, *, principal: Principal, include_live_connections: bool = False
) -> dict:
    """Return every permitted resource and capability matching ``query``."""

    registered_resources = resource_specs()
    registered_commands = capability_specs()
    registered_connections = connection_specs()
    permitted_connections = tuple(
        spec
        for spec in registered_connections
        if _permitted(spec.required_capabilities, principal)
    )
    # Registry discovery is a zero-query application primitive used by CLI,
    # MCP and contract checks. The web palette explicitly opts into cached live
    # instances because operator-entered names and current reachability are the
    # point of that surface; other adapters keep the original cheap contract.
    live_connections = (
        tuple(
            _live_connection_item(group, connection)
            for group in connection_catalog(principal=principal)
            for connection in group.connections
        )
        if include_live_connections
        else ()
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
            _command_item(
                spec,
                (
                    CommandRelation()
                    if query.casefold() == spec.name.casefold()
                    else related_commands.get(spec.name, CommandRelation())
                ),
            ),
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
        "resources": _matching(resources, query),
        "commands": (
            tuple(
                sorted(
                    commands,
                    key=lambda item: _match_score(item, query),
                    reverse=True,
                )
            )
            if query
            else commands
        ),
        "connections": _matching((*live_connections, *connections), query),
        "views": _matching(views, query),
        "checks": _matching(checks, query),
    }
