"""Operator discovery across HQ's declared resources and commands."""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import NoReverseMatch, reverse

from .capabilities import capability_specs
from .connections import connection_specs
from .resources import ResourceSpec, resource_specs
from .security import AuthorizationError, Capability, Principal


@dataclass(frozen=True)
class DiscoveryItem:
    kind: str
    name: str
    label: str
    summary: str
    url: str
    destination_label: str
    badges: tuple[str, ...]


def _permitted(required: tuple[Capability | str, ...], principal: Principal) -> bool:
    try:
        for capability in required:
            principal.require(capability)
    except AuthorizationError:
        return False
    return True


def _matches(item: DiscoveryItem, query: str) -> bool:
    terms = query.casefold().split()
    haystack = " ".join(
        (item.name, item.label, item.summary, *item.badges)
    ).casefold()
    return all(term in haystack for term in terms)


def _route_url(route: str) -> str:
    if not route:
        return ""
    try:
        return reverse(route)
    except NoReverseMatch:
        # The system check reports the broken plugin contract at startup. Keep
        # discovery usable if checks were skipped by an unusual process.
        return ""


def _resource_url(spec: ResourceSpec) -> str:
    return _route_url(spec.web_route)


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
            url=_route_url(spec.web_route),
            destination_label="",
            badges=(
                _ability_count(len(spec.abilities)),
                *((spec.secret_store,) if spec.secret_store else ()),
            ),
        )
        for spec in connection_specs()
        if _permitted(spec.required_capabilities, principal)
    )
    return {
        "resources": tuple(item for item in resources if _matches(item, query)),
        "commands": tuple(item for item in commands if _matches(item, query)),
        "connections": tuple(item for item in connections if _matches(item, query)),
    }
