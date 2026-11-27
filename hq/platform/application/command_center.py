"""Operator discovery across HQ's declared resources and commands."""

import re
from dataclasses import dataclass

from hq.domains.control_plane.names import normalized_hostname
from hq.platform.application.url_query import with_query

from .action_links import command_url
from .capabilities import CapabilitySpec, capability_registry
from .command_targets import starting_page
from .connection_catalog import connection_catalog
from .connections import ConnectionAbility, ConnectionSpec
from .contracts import route_url
from .entity_links import entity_link
from .facts import in_zone
from .finding_checks import check_url
from .findings import finding_rules
from .integrations import held_graph
from .labels import dotted
from .resources import ResourceSpec
from .security import Capability, Principal
from .topology_lenses import topology_lenses
from .ui import counted
lazy from .estate import estate_reading
lazy from .projection import projection_scope

_MATCHING_ABILITY_BADGE_LIMIT = 3
_SEARCH_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class DiscoveryItem:
    kind: str
    name: str
    label: str
    summary: str
    url: str
    destination_label: str
    badges: tuple[str, ...]
    search_terms: tuple[str, ...] = ()


def _matches(item: DiscoveryItem, query: str) -> bool:
    # An item is found by its own words. Its badges are its state and effect,
    # which many items share, so a search for one would find them all.
    return _contains_all((item.name, item.label, item.summary, *item.search_terms), query)


def _contains_all(values: tuple[str, ...], query: str) -> bool:
    terms = _SEARCH_WORD.findall(query.casefold())
    words = _SEARCH_WORD.findall(" ".join(values).casefold())
    # Two-letter input is a person beginning a word, not permission to match
    # the same two characters in the middle of everything. `sp` should find
    # splits and spending; it should never find infrastructure.
    return all(any(word.startswith(term) if len(term) <= 2 else term in word for word in words) for term in terms)


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
    # is context rather than the connection's own name. Searching `example`
    # should rank `example-npm` above every unrelated credential observed by a
    # controller named `example-host`.
    primary_name = item.name.rpartition(":")[2] if item.kind == "connection" and ":" in item.name else item.name
    return sum(
        max(
            *(_term_score(value, term) * 4 for value in (item.label, primary_name)),
            _term_score(item.summary, term) * 2,
            *(_term_score(value, term) for value in item.search_terms),
        )
        for term in terms
    )


def _ranked(items: tuple[DiscoveryItem, ...], query: str) -> tuple[DiscoveryItem, ...]:
    """The items best match first, or as declared when nothing was asked."""

    if not query:
        return items
    return tuple(sorted(items, key=lambda item: _match_score(item, query), reverse=True))


def _matching(items: tuple[DiscoveryItem, ...], query: str) -> tuple[DiscoveryItem, ...]:
    return _ranked(tuple(item for item in items if _matches(item, query)), query)


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
    labels = tuple(ability.label for ability in spec.abilities if _ability_contains_any(ability, query))
    visible = labels[:_MATCHING_ABILITY_BADGE_LIMIT]
    hidden = len(labels) - len(visible)
    if not hidden:
        return visible
    return (*visible, f"+{hidden} more")


def _command_item(spec: CapabilitySpec, executors: tuple[str, ...]) -> DiscoveryItem | None:
    """A command as search offers it: its form, or the page a person starts
    it from (``reached_from``); ``None`` for one only its own page sends."""

    url, destination = command_url(spec.name), ""
    if spec.reached_from:
        start = starting_page(spec)
        if start is None:
            return None
        destination, url = start
    return DiscoveryItem(
        kind="command",
        name=spec.name,
        label=spec.title,
        summary=spec.summary,
        url=url,
        destination_label=destination,
        badges=(spec.effect.label, *(f"via {label}" for label in executors)),
    )


def _executors(connections: tuple[ConnectionSpec, ...]) -> dict[str, tuple[str, ...]]:
    """Each command a connection's ability runs (``ConnectionAbility.capability``),
    to the labels of those abilities: what the command runs through."""

    found: dict[str, dict[str, None]] = {}
    for connection in connections:
        for ability in connection.abilities:
            if ability.capability:
                found.setdefault(ability.capability, {}).setdefault(ability.label, None)
    return {name: tuple(labels) for name, labels in found.items()}


def _resource_url(spec: ResourceSpec) -> str:
    return route_url(spec.web_route)


def _lens_url(name: str) -> str:
    base = route_url("control_plane:topology")
    return with_query(base, {"lens": name}) if base else ""


def _live_connection_url(group, connection) -> str:
    relationship = next(
        (action.url for action in connection.actions if action.name == "relationships"),
        "",
    )
    return relationship or route_url(group.spec.web_route)


def _live_connection_item(group, connection) -> DiscoveryItem:
    instance = connection.instance
    abilities = tuple(state.ability for state in connection.abilities)
    named = entity_link("connection", instance.label).label
    return DiscoveryItem(
        kind="connection",
        name=instance.id,
        label=named,
        # What kind of connection it is. Its address and its identifiers are
        # searched, and shown on its own page.
        summary=group.spec.label if group.spec.label != named else "",
        url=_live_connection_url(group, connection),
        destination_label="",
        badges=(connection.lifecycle.label,),
        search_terms=(
            instance.label,
            group.spec.label,
            group.spec.summary,
            instance.kind,
            instance.detail,
            instance.endpoint,
            instance.controller_id,
            *(fact.label for fact in instance.facts),
            *(fact.value for fact in instance.facts),
            *(target.label for target in instance.targets),
            *(dependency.label for dependency in instance.dependencies),
            *(ability.name for ability in abilities),
            *(ability.label for ability in abilities),
            *(ability.summary for ability in abilities),
            *connection.missing_scopes,
        ),
    )


# Among equally good matches: the machine, then the service, then the domain.
_ESTATE_ORDER = {"machine": 0, "service": 1, "zone": 2}
# An exact name or address outranks any number of partial word matches.
_EXACT = 1000


def _estate_items() -> tuple[DiscoveryItem, ...]:
    """Machines, services and domains, from the catalogs their pages read."""

    estate = estate_reading()
    machines = tuple(
        DiscoveryItem(
            kind="machine",
            name=machine.name,
            label=machine.name,
            summary=dotted(
                ", ".join(role.label for role in machine.roles),
                machine.role,
                counted(len(machine.hostnames), "service") if machine.hostnames else "",
            ),
            url=machine.url,
            destination_label="Machine",
            badges=(machine.state.label,),
            search_terms=tuple(
                term
                for term in (
                    *machine.aliases,
                    machine.declaration,
                    *((machine.presence.tailnet_name, machine.presence.dns_name) if machine.presence else ()),
                    *machine.addresses,
                    *machine.public_addresses,
                )
                if term
            ),
        )
        for machine in estate.machines
    )
    services = tuple(
        DiscoveryItem(
            kind="service",
            name=service.hostname,
            label=service.hostname,
            summary=service.origin.headline if service.origin else "",
            url=service.url,
            destination_label="Service",
            badges=(),
            search_terms=tuple(service.aliases),
        )
        for service in estate.services
    )
    domains = tuple(
        DiscoveryItem(
            kind="zone",
            name=domain,
            label=domain,
            summary="Domain",
            url=entity_link("zone", domain).url,
            destination_label="Domain",
            badges=(),
        )
        for domain in estate.domains
    )
    return (*machines, *services, *domains)


def _exact(item: DiscoveryItem, query: str) -> bool:
    wanted = normalized_hostname(query)
    return wanted in {normalized_hostname(value) for value in (item.name, item.label, *item.search_terms)}


def _holds_name(item: DiscoveryItem, query: str) -> bool:
    """A domain holds every hostname under it."""

    return item.kind == "zone" and in_zone(query, normalized_hostname(item.name))


def _matching_estate(items: tuple[DiscoveryItem, ...], query: str) -> tuple[DiscoveryItem, ...]:
    if not query.strip():
        return ()
    scored = [
        (
            (_EXACT if _exact(item, query) else 0) + _match_score(item, query),
            item,
        )
        for item in items
        if _matches(item, query) or _holds_name(item, query)
    ]
    return tuple(
        item for _score, item in sorted(scored, key=lambda pair: (-pair[0], _ESTATE_ORDER.get(pair[1].kind, 9)))
    )


def estate_search(query: str, *, principal: Principal) -> tuple[DiscoveryItem, ...]:
    """Machines, services and domains matching ``query``, best first."""

    if not query.strip() or not principal.permits(Capability.READ):
        return ()
    return _matching_estate(_estate_items(), query)


def command_center(query: str, *, principal: Principal, include_live_connections: bool = False) -> dict:
    """Return every permitted resource and capability matching ``query``."""

    with projection_scope():
        return _command_center(query, principal=principal, include_live_connections=include_live_connections)


def _command_center(query: str, *, principal: Principal, include_live_connections: bool) -> dict:

    graph = held_graph()
    permitted_connections = tuple(spec for spec in graph.connections.values() if spec.permitted_to(principal))
    # Registry discovery is a zero-query application primitive used by CLI,
    # MCP and contract checks. The web palette explicitly opts into cached live
    # instances because operator-entered names and current reachability are the
    # point of that surface; other adapters keep the cheap contract. The estate
    # is live state too: only the palette and the search page ask for it.
    live_connections = _live_connection_items(principal) if include_live_connections else ()
    estate = estate_search(query, principal=principal) if include_live_connections else ()
    views, checks = _questions(principal)
    return {
        "estate": estate,
        "resources": _matching(_resource_items(tuple(graph.resources.values()), principal), query),
        "commands": _matching(_command_items(capability_registry(), permitted_connections, principal), query),
        "connections": _matching((*live_connections, *_connection_items(permitted_connections, query)), query),
        "views": _matching(views, query),
        "checks": _matching(checks, query),
    }


def _live_connection_items(principal: Principal) -> tuple[DiscoveryItem, ...]:
    return tuple(
        _live_connection_item(group, connection)
        for group in connection_catalog(principal=principal)
        for connection in group.connections
    )


def _resource_items(registered: tuple[ResourceSpec, ...], principal: Principal) -> tuple[DiscoveryItem, ...]:
    return tuple(
        DiscoveryItem(
            kind="resource",
            name=spec.name,
            label=spec.label,
            summary=spec.summary,
            url=_resource_url(spec),
            destination_label="",
            badges=(),
        )
        for spec in registered
        if spec.permitted_to(principal)
    )


def _command_items(
    registered: tuple[CapabilitySpec, ...],
    permitted_connections: tuple[ConnectionSpec, ...],
    principal: Principal,
) -> tuple[DiscoveryItem, ...]:
    """The permitted commands search can open."""

    executors = _executors(permitted_connections)
    return tuple(
        item
        for spec in registered
        if spec.permitted_to(principal)
        if (item := _command_item(spec, executors.get(spec.name, ()))) is not None
    )


def _connection_items(permitted: tuple[ConnectionSpec, ...], query: str) -> tuple[DiscoveryItem, ...]:
    return tuple(
        DiscoveryItem(
            kind="connection",
            name=spec.name,
            label=spec.label,
            summary=spec.summary,
            url=route_url(spec.web_route),
            destination_label="",
            badges=_matching_ability_labels(spec, query),
            search_terms=tuple(
                term for ability in spec.abilities for term in (ability.name, ability.label, ability.summary)
            ),
        )
        for spec in permitted
    )


def _questions(principal: Principal) -> tuple[tuple[DiscoveryItem, ...], tuple[DiscoveryItem, ...]]:
    """The topology lenses and the finding rules a reader may open.

    Each is a question about the graph rather than a thing in it, so it costs
    no query to offer: the declarations are static and the projection is
    derived only once the operator opens one. A rule is offered, not the claims
    it would make, which would derive the whole projection on every keystroke.
    """

    if not principal.permits(Capability.READ):
        return (), ()
    views = tuple(
        DiscoveryItem(
            kind="view",
            name=lens.name,
            label=lens.label,
            summary=lens.summary,
            url=_lens_url(lens.name),
            destination_label="",
            badges=(),
        )
        for lens in topology_lenses()
    )
    checks = tuple(
        DiscoveryItem(
            kind="check",
            name=rule.name,
            label=rule.title,
            summary="",
            url=check_url(rule.name),
            destination_label="",
            badges=(rule.severity.urgency,),
        )
        for rule in finding_rules()
    )
    return views, checks
