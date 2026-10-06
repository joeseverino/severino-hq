"""Everything HQ derives about one managed resource, for every adapter.

The resource page, the HTTP API and MCP's ``get_resource`` read this
one projection, so none of them can derive a fact the others cannot return:
what may be done to it, where it runs and sends traffic, which services it
takes part in, how its provider describes it, and when a certificate runs out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.control_plane.provider_adapters.declarations import DELIVERY_TARGET_KIND
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND

from .entity_links import entity_link
from .expiry import certificate_expiry, days_until, renewal_opens_at, renewal_window
from .infrastructure import (
    NotFoundError,
    controller_contract,
    resolved_spec,
    resource_health,
    serialize_resource,
)
from .resource_operations import resource_history
from .resource_capabilities import ResourceCapabilities, resource_capabilities


def origin_machine(resource, machines=None, at=None, targets=None):
    """The machine a resource forwards to, if its provider says where it serves.

    The readings are passed in by a page asking this of every row, where taking
    them here is the same four queries repeated once per resource.
    """

    from .whereabouts import machine_link

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


# How far behind the newest reading of its own type a record may fall before
# it reads as not seen. The lens and the rule that ask the same question share
# the line (``topology_lenses._STALE_AFTER``).
def _left_behind(read_at: datetime | None, newest: datetime | None) -> bool:
    from .topology_lenses import _STALE_AFTER

    return read_at is not None and newest is not None and newest - read_at > _STALE_AFTER


@dataclass(frozen=True)
class RecordStatus:
    """One record's state as one line: what it is, since when, what happens next.

    The only status a record page or a list row shows, so two statuses can
    never disagree side by side.
    """

    state: str
    # The ``control-*`` tone the label is drawn in.
    tone: str
    label: str
    detail: str = ""
    since: datetime | None = None
    read_at: datetime | None = None


def _reconciles(kind: str) -> tuple[bool, bool]:
    """``(applies, automatically)``: whether the controller applies HQ's settings."""

    from hq.domains.control_plane.providers import controller_capability_registry

    capability = controller_capability_registry().capabilities.get(kind)
    policy = capability.actions.get("reconcile") if capability else None
    applies = bool(policy and policy.mode == "apply")
    return applies, bool(applies and policy.automatic)


def _fault_status(resource, health: dict[str, str]) -> RecordStatus | None:
    """A record that is switched off, changed outside HQ, or reporting a problem."""

    from .conditions import held_since

    read_at = resource.last_observed_at
    if not resource.enabled:
        return RecordStatus(
            "off", "declared", "Switched off in HQ", "HQ does not apply or check it.", None, read_at
        )
    if health["state"] == "drifted":
        return RecordStatus(
            "drifted",
            "drifted",
            health["label"],
            f"{health['message']} Keep the live version, or restore HQ's version.".strip(),
            held_since(resource.conditions, "Drifted"),
            read_at,
        )
    if health["state"] == "deploying":
        return RecordStatus(
            "deploying",
            "pending",
            health["label"],
            health["message"],
            held_since(resource.conditions, "Degraded"),
            read_at,
        )
    if health["state"] == "degraded":
        return RecordStatus(
            "degraded",
            "degraded",
            health["label"],
            health["message"],
            held_since(resource.conditions, "Degraded"),
            read_at,
        )
    return None


def record_status(
    resource, *, health: dict[str, str] | None = None, newest: datetime | None = None
) -> RecordStatus:
    """``newest`` is the latest reading of any record of this type: a record
    read long before it was not found the last time HQ looked."""

    health = health or resource_health(resource)
    fault = _fault_status(resource, health)
    if fault is not None:
        return fault
    read_at = resource.last_observed_at
    applies, automatically = _reconciles(resource.kind)
    if applies and resource.observed_generation != resource.generation:
        return RecordStatus(
            "pending",
            "pending",
            "Change waiting to apply",
            "The controller applies it within a few minutes."
            if automatically
            else "Press Apply again to apply it.",
            None,
            read_at,
        )
    if _left_behind(read_at, newest):
        return RecordStatus(
            "unseen",
            "unknown",
            "Not found lately",
            "The others of its type have been read since.",
            None,
            read_at,
        )
    if health["state"] == "healthy":
        return RecordStatus(
            "working",
            "healthy",
            health["label"],
            "Matches HQ's settings." if applies else "",
            None,
            read_at,
        )
    if not applies:
        return RecordStatus(
            "recorded", "declared", "Recorded only", "HQ only records this. Nothing is applied.", None, read_at
        )
    return RecordStatus("unread", "unknown", health["label"], health["message"], None, read_at)


def newest_reading(kind: str) -> datetime | None:
    """When any tracked record of this type was last read, from the read a
    page already shares (``infrastructure.enabled_resources``)."""

    from .infrastructure import enabled_resources

    return max(
        (
            resource.last_observed_at
            for resource in enabled_resources()
            if resource.kind == kind and resource.last_observed_at
        ),
        default=None,
    )


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
    # A certificate's services, and its names nothing is served under.
    certificate_use: Any = None

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
            "used_by": list(self.certificate_use.used_by) if self.certificate_use else [],
            "unused_names": list(self.certificate_use.unused) if self.certificate_use else [],
        }


def get_managed_resource(key: str) -> dict[str, Any]:
    """Return resource state, what HQ derives from it, and operation history."""

    try:
        resource = ManagedResource.objects.get(key=key)
    except ManagedResource.DoesNotExist as exc:
        raise NotFoundError(f"No record named {key!r}.") from exc
    return {
        "resource": serialize_resource(resource),
        "derived": resource_context(resource).as_dict(),
        "operations": resource_history(resource),
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
        certificate_use=_certificate_use(resource, resolved),
    )


def _certificate_use(resource, resolved: dict[str, Any] | None):
    """What a certificate serves; None for anything that is not one."""

    provider = PROVIDERS.get(resource.kind)
    if provider is None or not provider.covers:
        return None
    from .services import certificate_use

    return certificate_use(resource.key, provider.hostnames(resolved or resource.spec) or ())


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
    # Each reason is a whole sentence about the record, so it stands alone.
    lines = tuple(off)
    if len(off) and sum(len(names) for names in off.values()) == len(actions):
        return ControllerSummary("HQ does not change this", "declared", lines, frozenset(off))
    automatic = any(allowed.automatic for allowed in actions.values())
    return ControllerSummary(
        "Applied automatically" if automatic else "Applied when you press Apply again",
        "good",
        lines,
        frozenset(off),
    )


# The tones a row is drawn in when nothing about it needs looking at.
SETTLED_TONES = frozenset({"healthy", "declared"})


@dataclass(frozen=True)
class RecordGroup:
    """The records of one type, those needing a look first."""

    kind: str
    label: str
    rows: tuple[Any, ...]
    unsettled: int


@dataclass(frozen=True)
class RecordList:
    """The infrastructure list under its types, and what the filter offers.

    ``types`` is every type there is with how many records it has, whatever
    the filter keeps, so a filtered page still offers the others.
    """

    groups: tuple[RecordGroup, ...]
    types: tuple[RecordGroup, ...]
    query: str = ""
    kind: str = ""

    @property
    def total(self) -> int:
        return sum(len(group.rows) for group in self.types)

    @property
    def shown(self) -> int:
        return sum(len(group.rows) for group in self.groups)

    @property
    def unsettled(self) -> int:
        return sum(group.unsettled for group in self.types)

    @property
    def filtered(self) -> bool:
        return bool(self.query or self.kind)


def _grouped(rows: list[Any]) -> tuple[RecordGroup, ...]:
    found: dict[str, list[Any]] = {}
    for row in rows:
        found.setdefault(row.kind, []).append(row)
    groups = []
    for kind, members in found.items():
        unsettled = [row for row in members if row.record_status.tone not in SETTLED_TONES]
        settled = [row for row in members if row.record_status.tone in SETTLED_TONES]
        groups.append(
            RecordGroup(kind, members[0].kind_label, tuple(unsettled + settled), len(unsettled))
        )
    # A type with something to look at leads; the rest by name.
    return tuple(sorted(groups, key=lambda group: (not group.unsettled, group.label.casefold())))


def record_list(resources, *, query: str = "", kind: str = "") -> RecordList:
    """``resources`` (each carrying the list row's ``record_status``,
    ``shown_name`` and ``summary``) under their types, kept by the filter:
    words in a row's own text, and one type."""

    rows = list(resources)
    types = _grouped(rows)
    wanted = query.strip().casefold()
    kind = kind if any(group.kind == kind for group in types) else ""
    kept = [
        row
        for row in rows
        if (not kind or row.kind == kind)
        and (
            not wanted
            or wanted
            in " ".join(
                (row.key, row.shown_name, row.summary, row.kind_label, row.record_status.label)
            ).casefold()
        )
    ]
    return RecordList(_grouped(kept), types, query.strip(), kind)
