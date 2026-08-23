"""Operator discovery across HQ's declared resources and commands."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from .capabilities import capability_specs
from .connections import ConnectionSpec, connection_specs
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


def _matching_ability_labels(spec: ConnectionSpec, query: str) -> tuple[str, ...]:
    """Name why a connection matched instead of showing an opaque family hit."""

    terms = query.casefold().split()
    if not terms:
        return ()
    labels = tuple(
        ability.label
        for ability in spec.abilities
        if any(
            term
            in " ".join((ability.name, ability.label, ability.summary)).casefold()
            for term in terms
        )
    )
    visible = labels[:_MATCHING_ABILITY_BADGE_LIMIT]
    hidden = len(labels) - len(visible)
    if not hidden:
        return visible
    return (*visible, f"+{hidden} matching abilities")


def _resource_url(spec: ResourceSpec) -> str:
    return route_url(spec.web_route)


def _lens_url(name: str) -> str:
    base = route_url("control_plane:topology")
    return f"{base}?{urlencode({'lens': name})}" if base else ""


def _finding_url(name: str) -> str:
    base = route_url("control_plane:topology")
    return f"{base}?{urlencode({'finding': name})}" if base else ""


def _command_label(name: str) -> str:
    words = name.replace(".", " ").replace("_", " ").split()
    return " ".join(word.upper() if len(word) <= 3 else word.title() for word in words)


def _ability_count(count: int) -> str:
    return f"{count} {'ability' if count == 1 else 'abilities'}"


def command_center(query: str, *, principal: Principal) -> dict:
    """Return every permitted resource and capability matching ``query``."""

    registered_resources = resource_specs()
    resource_by_name = {spec.name: spec for spec in registered_resources}
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
        DiscoveryItem(
            kind="command",
            name=spec.name,
            label=_command_label(spec.name),
            summary=spec.summary,
            url=(
                _resource_url(resource_by_name[spec.subject_resource])
                if spec.subject_resource in resource_by_name
                else ""
            ),
            destination_label=(
                resource_by_name[spec.subject_resource].label
                if spec.subject_resource in resource_by_name
                else ""
            ),
            badges=(spec.effect.replace("_", " "),),
        )
        for spec in capability_specs()
        if _permitted(spec.required_capabilities, principal)
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
        for spec in connection_specs()
        if _permitted(spec.required_capabilities, principal)
    )
    # A lens is a question about the graph rather than a thing in it, so it
    # costs no query to offer: the declarations are static and the projection is
    # derived only once the operator opens one.
    views = (
        tuple(
            DiscoveryItem(
                kind="view", name=lens.name, label=lens.label, summary=lens.summary,
                url=_lens_url(lens.name), destination_label="Topology",
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
                kind="check", name=rule.name, label=rule.title, summary=rule.severity,
                url=_finding_url(rule.name), destination_label="Findings",
                badges=(rule.severity,),
            )
            for rule in finding_rules()
        )
        if _permitted((Capability.READ,), principal)
        else ()
    )
    return {
        "resources": tuple(item for item in resources if _matches(item, query)),
        "commands": tuple(item for item in commands if _matches(item, query)),
        "connections": tuple(item for item in connections if _matches(item, query)),
        "views": tuple(item for item in views if _matches(item, query)),
        "checks": tuple(item for item in checks if _matches(item, query)),
    }
