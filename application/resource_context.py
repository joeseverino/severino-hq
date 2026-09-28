"""Everything HQ derives about one managed resource, for every adapter.

The resource page, the HTTP API and MCP's ``get_managed_resource`` read this
one projection, so none of them can derive a fact the others cannot return:
what may be done to it, where it runs and sends traffic, which services it
takes part in, how its provider describes it, and when a certificate runs out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from control_plane.models import ManagedResource
from control_plane.providers import PROVIDERS
from control_plane.provider_adapters.declarations import DELIVERY_TARGET_KIND
from control_plane.provider_adapters.tls import CERTIFICATE_KIND

from .entity_links import entity_link
from .expiry import certificate_expiry, days_until, renewal_opens_at, renewal_window
from .infrastructure import (
    NotFoundError,
    controller_contract,
    operation_summary,
    resolved_spec,
    resource_health,
    serialize_resource,
)
from .resource_capabilities import ResourceCapabilities, resource_capabilities


def origin_machine(resource, machines=None, at=None, targets=None):
    """The machine a resource forwards to, if its provider says where it serves.

    The readings are passed in by a page asking this of every row, where taking
    them here is the same four queries repeated once per resource.
    """

    from .services import machine_link

    provider = PROVIDERS.get(resource.kind)
    if provider is None or provider.origin is None:
        return None
    try:
        origin = provider.origin(resolved_spec(resource, targets))
    except (KeyError, TypeError, ValueError):
        return None
    return machine_link(origin, machines, at) if origin else None


def provider_machine(resource):
    """The one machine hosting the provider that manages this resource.

    The resource's origin is where it sends traffic. The provider connection
    is where the proxy, DNS server, or controller itself runs.
    """

    from .connections import connection_readings
    from .machines import machine as machine_named

    provider = PROVIDERS.get(resource.kind)
    if provider is None:
        return None
    matches = {
        found.name
        for reading in connection_readings()
        if reading.provider in provider.connection_providers
        if (found := machine_named(reading.controller_id)) is not None
    }
    if len(matches) != 1:
        return None
    link = entity_link("machine", matches.pop())
    return {"name": link.label, "url": link.url, "link": link}


def service_links(resource) -> tuple[tuple[str, str], ...]:
    """``(hostname, url)`` for every service this resource takes part in."""

    provider = PROVIDERS[resource.kind]
    if provider.hostnames is None:
        return ()
    try:
        names = provider.hostnames(resolved_spec(resource))
    except (KeyError, TypeError, ValueError):
        return ()
    links = ((name, entity_link("service", name).url) for name in names)
    return tuple((name, url) for name, url in links if url)


def readout_rows(resource) -> tuple[tuple[str, str, str], ...]:
    """``(label, desired, observed)`` as the provider describes itself."""

    provider = PROVIDERS[resource.kind]
    if provider.readout is None:
        return ()
    try:
        return tuple(provider.readout(resource.spec, resource.status or {}))
    except (KeyError, TypeError, ValueError):
        return ()


@dataclass(frozen=True)
class Expiry:
    not_after: datetime
    days_left: int
    renewal_at: datetime


def expiry_of(resource) -> Expiry | None:
    expires = certificate_expiry(resource.status)
    if expires is None:
        return None
    return Expiry(
        expires,
        max(0, days_until(expires)),
        renewal_opens_at(expires, renewal_window(resource.spec)),
    )


def _consumers(resource) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...], str]:
    """``(resolved spec, consumers with their observed names, error)`` of a certificate."""

    if resource.kind != CERTIFICATE_KIND:
        return None, (), ""
    try:
        resolved = controller_contract(resource)["resource"]["spec"]
    except (KeyError, ValueError) as exc:
        return resource.spec, (), str(exc)
    observed: dict[str, set[str]] = {}
    for observation in resource.status.get("consumers", []):
        observed.setdefault(observation.get("consumer", ""), set()).add(
            observation.get("domain", "")
        )
    targets = {
        target.spec.get("connection_ref"): target.key
        for target in ManagedResource.objects.filter(kind=DELIVERY_TARGET_KIND, enabled=True)
    }
    consumers = tuple(
        {
            **consumer,
            "url": (
                entity_link("resource", targets[consumer["connection_ref"]]).url
                if consumer.get("connection_ref") in targets
                else ""
            ),
            "display_domains": sorted(
                domain for domain in observed.get(consumer["name"], set()) if domain
            )
            or consumer.get("verify_domains", []),
        }
        for consumer in resolved.get("consumers", ())
    )
    return resolved, consumers, ""


@dataclass(frozen=True)
class ResourceContext:
    resource: ManagedResource
    capabilities: ResourceCapabilities
    health: dict[str, str]
    origin_machine: Any
    provider_machine: dict[str, Any] | None
    service_links: tuple[tuple[str, str], ...]
    readout_rows: tuple[tuple[str, str, str], ...]
    expiry: Expiry | None
    awaiting_approval: tuple[Any, ...]
    resolved_spec: dict[str, Any] | None
    display_consumers: tuple[dict[str, Any], ...]
    resolution_error: str

    @property
    def in_sync(self) -> bool:
        return self.resource.generation == self.resource.observed_generation

    def as_dict(self) -> dict[str, Any]:
        """The derived facts, for the API and MCP."""

        origin = self.origin_machine
        return {
            "health": self.health,
            "in_sync": self.in_sync,
            "actions": {
                verb: {
                    "enabled": allowed.enabled,
                    "reason": allowed.reason,
                    "automatic": allowed.automatic,
                }
                for verb, allowed in self.capabilities.actions.items()
            },
            "removal": {
                "mode": self.capabilities.removal,
                "reason": self.capabilities.removal_reason,
                "pending": self.capabilities.removal_pending,
            },
            "origin_machine": (
                {"name": origin.name, "url": origin.url} if origin is not None else None
            ),
            "provider_machine": (
                {
                    "name": self.provider_machine["name"],
                    "url": self.provider_machine["url"],
                }
                if self.provider_machine
                else None
            ),
            "services": [
                {"hostname": hostname, "url": url} for hostname, url in self.service_links
            ],
            "readout": [
                {"label": label, "desired": desired, "observed": observed}
                for label, desired, observed in self.readout_rows
            ],
            "expiry": (
                {
                    "not_after": self.expiry.not_after.isoformat(),
                    "days_left": self.expiry.days_left,
                    "renewal_at": self.expiry.renewal_at.isoformat(),
                }
                if self.expiry
                else None
            ),
            "awaiting_approval": len(self.awaiting_approval),
            "consumers": [
                {
                    "name": consumer.get("name", ""),
                    "kind": consumer.get("kind", ""),
                    "domains": list(consumer.get("display_domains") or ()),
                }
                for consumer in self.display_consumers
            ],
            "resolution_error": self.resolution_error,
        }


def get_managed_resource(key: str) -> dict[str, Any]:
    """Return resource state, what HQ derives from it, and operation history."""

    try:
        resource = ManagedResource.objects.get(key=key)
    except ManagedResource.DoesNotExist as exc:
        raise NotFoundError(f"Managed resource {key!r} was not found.") from exc
    return {
        "resource": serialize_resource(resource),
        "derived": resource_context(resource).as_dict(),
        "operations": [
            operation_summary(operation) for operation in resource.operations.all()[:20]
        ],
    }


def resource_context(resource: ManagedResource) -> ResourceContext:
    """Every fact HQ derives about one resource, derived once."""

    from .approvals import pending

    resolved, consumers, error = _consumers(resource)
    return ResourceContext(
        resource=resource,
        capabilities=resource_capabilities(resource),
        health=resource_health(resource),
        origin_machine=origin_machine(resource),
        provider_machine=provider_machine(resource),
        service_links=service_links(resource),
        readout_rows=readout_rows(resource),
        expiry=expiry_of(resource),
        awaiting_approval=tuple(
            held for held in pending() if held.resource_key == resource.key
        ),
        resolved_spec=resolved,
        display_consumers=consumers,
        resolution_error=error,
    )


@dataclass(frozen=True)
class ControllerSummary:
    """What the controller will do for a resource, said once."""

    headline: str
    tone: str
    lines: tuple[str, ...]
    # The reasons already given, so a card beside this one need not repeat them.
    reasons: frozenset[str]


def controller_summary(actions, labels) -> ControllerSummary | None:
    """``actions``: verb -> allowance. Actions that are off for the same reason
    are named together, once; and a controller that can do nothing here is
    "observing only", never "automatic"."""

    if not actions:
        return None
    off: dict[str, list[str]] = {}
    for verb, allowed in actions.items():
        if not allowed.enabled:
            off.setdefault(allowed.reason, []).append(labels(verb))
    lines = tuple(
        f"{_and(names)} {'is' if len(names) == 1 else 'are'} off: {reason[:1].lower() + reason[1:]}"
        for reason, names in off.items()
    )
    if len(off) and sum(len(names) for names in off.values()) == len(actions):
        return ControllerSummary("Observing only", "declared", lines, frozenset(off))
    automatic = any(allowed.automatic for allowed in actions.values())
    return ControllerSummary("Automatic" if automatic else "On request", "good", lines, frozenset(off))


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
