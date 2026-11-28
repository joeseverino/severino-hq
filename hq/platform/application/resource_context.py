"""Everything HQ derives about one managed resource, for every adapter.

The resource page, the HTTP API and MCP's ``get_resource`` read this
one projection, so none of them can derive a fact the others cannot return:
what may be done to it, where it runs and sends traffic, which services it
takes part in, how its provider describes it, and when a certificate runs out.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils.text import Truncator

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.observations.portainer import CONTAINER_KIND
from hq.domains.control_plane.provider_adapters.declarations import DELIVERY_TARGET_KIND
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND
from hq.domains.control_plane.providers import PROVIDERS, read_spec, readout_rows

from .asks import APPLIES_SHORTLY
from .display_words import as_members
from .entity_links import entity_link, kind_label, record_name
from .expiry import certificate_expiry, days_until, renewal_opens_at, renewal_window
from .infrastructure import (
    RecordState,
    ResourceState,
    controller_contract,
    declared_machines,
    delivery_targets,
    resolved_spec,
    resource_health,
    serialize_resource,
)
from .record_errors import RecordNotFound
from .resource_capabilities import ResourceCapabilities, kind_converges, resource_capabilities
from .resource_operations import resource_history
from .tone import Tone
lazy from . import machines
lazy from .approvals import pending
lazy from .conditions import DEGRADED, DRIFTED, held_since
lazy from .connections import connection_readings
lazy from .managed_records import ManagedResourceListed, ManagedResourceRecord
lazy from .projection import iso, page_of, read_once
lazy from .security import internal_principal
lazy from .services import certificate_use
lazy from .whereabouts import machine_link, whereabouts


def origin_machine(resource, machines=None, at=None, targets=None):
    """The machine a resource forwards to, if its provider says where it serves.

    The readings are passed in by a page asking this of every row, where taking
    them here is the same four queries repeated once per resource.
    """

    provider = PROVIDERS.get(resource.kind)
    if provider is None or provider.origin is None:
        return None
    origin = read_spec(resource.kind, resolved_spec(resource, targets), provider.origin)
    return machine_link(origin, machines, at) if origin else None


def provider_machine(resource):
    """The one machine hosting the provider that manages this resource.

    The resource's origin is where it sends traffic. The provider connection
    is where the proxy, DNS server, or controller itself runs.
    """

    provider = PROVIDERS.get(resource.kind)
    if provider is None:
        return None
    matches = {
        found.name
        for reading in connection_readings()
        if reading.provider in provider.connection_providers
        if (found := machines.machine(reading.controller_id)) is not None
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
    names = read_spec(resource.kind, resolved_spec(resource), lambda spec: tuple(provider.hostnames(spec))) or ()
    links = ((name, entity_link("service", name).url) for name in names)
    return tuple((name, url) for name, url in links if url)


def record_summary(resource, shown_name: str) -> str:
    """What a record holds, in a line short enough to recognize it by.

    The first line of its provider's readout, shortened: a DMARC record runs
    to three wrapped lines and the full value is one click away on the record
    itself.
    """

    if not (rows := readout_rows(resource)):
        return ""
    label, desired, observed = rows[0]
    # Said with its label, as the record's search line says it: a bare "1" is a count of nothing.
    said = value_summary(desired or observed, shown_name)
    return f"{label}: {said}" if said else ""


def value_summary(value: str, shown_name: str) -> str:
    """A record's value as the line it is recognized by: shortened, and blank
    when the name it is shown by already says it, since "a → 10.0.0.10:80"
    needs no "http://10.0.0.10:80"."""

    if value.rpartition("://")[2] in shown_name:
        return ""
    return Truncator(value).chars(_SUMMARY_CHARS)


def shared_names[Name](names: Iterable[Name]) -> frozenset[Name]:
    """The names more than one record goes by: there, only the value tells them apart."""

    seen: set[Name] = set()
    shared: set[Name] = set()
    for name in names:
        (shared if name in seen else seen).add(name)
    return frozenset(shared)


_SUMMARY_CHARS = 64


@dataclass(frozen=True, slots=True)
class RecordStatus:
    """One record's state as one line: what it is, since when, what it means.

    The only status a record page or a list row shows, so two statuses can
    never disagree side by side.
    """

    # Its tone (``RecordState.tone``) draws the label and its ``detail``.
    state: RecordState
    label: str
    detail: str = ""
    since: datetime | None = None
    read_at: datetime | None = None

    def __post_init__(self) -> None:
        as_members(self)

    @property
    def tone(self) -> Tone:
        return self.state.tone


def _fault_status(resource, health: dict[str, str]) -> RecordStatus | None:
    """A record that is switched off, changed outside HQ, or reporting a problem."""

    read_at = resource.last_observed_at
    state = ResourceState(health["state"])
    if state is ResourceState.OFF:
        return RecordStatus(RecordState.OFF, health["label"], health["message"], None, read_at)
    if state is ResourceState.DRIFTED:
        return RecordStatus(
            RecordState.DRIFTED,
            health["label"],
            f"{health['message']} Keep the live version, or restore HQ's version.".strip(),
            held_since(resource.conditions, DRIFTED),
            read_at,
        )
    if state in (ResourceState.DEPLOYING, ResourceState.DEGRADED):
        return RecordStatus(
            RecordState(state),
            health["label"],
            health["message"],
            held_since(resource.conditions, DEGRADED),
            read_at,
        )
    return None


def record_status(resource, *, health: dict[str, str] | None = None) -> RecordStatus:
    """Where one record stands: its health (``resource_health``) in the words
    its page says it."""

    health = health or resource_health(resource)
    fault = _fault_status(resource, health)
    if fault is not None:
        return fault
    read_at = resource.last_observed_at
    applies = kind_converges(resource.kind)
    state = ResourceState(health["state"])
    if state is ResourceState.PENDING:
        return RecordStatus(
            RecordState.PENDING,
            health["label"],
            APPLIES_SHORTLY
            if applies and applies.automatic
            else f"Press {OperationRequest.Action.RECONCILE.label} to apply it.",
            None,
            read_at,
        )
    if state in (ResourceState.UNSEEN, ResourceState.STALE):
        return RecordStatus(RecordState(state), health["label"], health["message"], None, read_at)
    if state is ResourceState.HEALTHY:
        return RecordStatus(
            RecordState.WORKING,
            health["label"],
            "Matches HQ's config." if applies else "",
            None,
            read_at,
        )
    if not applies:
        return RecordStatus(
            RecordState.RECORDED, "Recorded only", "HQ only records this. Nothing is applied.", None, read_at
        )
    return RecordStatus(RecordState.UNREAD, health["label"], health["message"], None, read_at)


@dataclass(frozen=True, slots=True)
class ExpiryWindow:
    not_after: datetime
    days_left: int
    renewal_at: datetime


def expiry_of(resource) -> ExpiryWindow | None:
    expires = certificate_expiry(resource.status)
    if expires is None:
        return None
    return ExpiryWindow(
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
        observed.setdefault(observation.get("consumer", ""), set()).add(observation.get("domain", ""))
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
            "display_domains": sorted(domain for domain in observed.get(consumer["name"], set()) if domain)
            or consumer.get("verify_domains", []),
        }
        for consumer in resolved.get("consumers", ())
    )
    return resolved, consumers, ""


def listed(resource: ManagedResource, machines=None, at=None, targets=None) -> ManagedResourceListed:
    """``resource`` as its list's row says it, and its page's record begins:
    whole, and where it stands, sends traffic and what it holds. A list
    passes in what ``origin_machine`` reads for every row."""

    item = serialize_resource(resource)
    standing = record_status(resource, health=item["health"])
    origin = origin_machine(resource, machines, at, targets)
    name = page_name(resource)
    return {
        **item,
        "url": resource.get_absolute_url(),
        "name": name,
        "summary": record_summary(resource, name),
        "standing": {
            "state": str(standing.state),
            "tone": str(standing.state.tone),
            "label": standing.label,
            "detail": standing.detail,
            "since": iso(standing.since),
            "read_at": iso(standing.read_at),
        },
        "origin_machine": {"name": origin.name, "url": origin.url} if origin is not None else None,
    }


def list_managed_resources(*, limit: int = 50, kind: str | None = None) -> dict[str, Any]:
    """List canonical public infrastructure state without provider credentials, by kind then key."""
    resources = ManagedResource.objects.all()
    if kind:
        resources = resources.filter(kind=kind)
    sourced = [name for name, provider in PROVIDERS.items() if provider.sourced is not None]
    retired = [each.key for each in resources.filter(kind__in=sourced) if PROVIDERS[each.kind].retired(each)]
    resources = resources.exclude(key__in=retired)
    # Read once for the whole page: asked per row, where each forwards is a
    # query budget that grows with the estate.
    machines, targets = declared_machines(), delivery_targets()
    at = whereabouts(machines)
    return page_of(
        resources, limit, order=("kind", "key"), each=lambda resource: listed(resource, machines, at, targets)
    )


@dataclass(frozen=True, slots=True)
class ResourceContext:
    resource: ManagedResource
    capabilities: ResourceCapabilities
    provider_machine: dict[str, Any] | None
    service_links: tuple[tuple[str, str], ...]
    readout_rows: tuple[tuple[str, str, str], ...]
    expiry: ExpiryWindow | None
    awaiting_approval: tuple[Any, ...]
    resolved_spec: dict[str, Any] | None
    display_consumers: tuple[dict[str, Any], ...]
    resolution_error: str
    # A certificate's services, and its names nothing is served under.
    certificate_use: Any = None

    def record(self) -> ManagedResourceRecord:
        """The record whole with what HQ derives from it, as the API, MCP and its page read it."""

        resource, provider, expiry = self.resource, self.provider_machine, self.expiry
        resolved = self.resolved_spec if resource.kind == CERTIFICATE_KIND else None
        return {
            **listed(resource),
            "actions": {
                verb: {"enabled": allowed.enabled, "reason": allowed.reason, "automatic": allowed.automatic}
                for verb, allowed in self.capabilities.actions.items()
            },
            "removal": {
                "mode": self.capabilities.removal,
                "reason": self.capabilities.removal_reason,
                "pending": self.capabilities.removal_pending,
            },
            "provider_machine": {"name": provider["name"], "url": provider["url"]} if provider else None,
            "services": [{"hostname": hostname, "url": url} for hostname, url in self.service_links],
            "readout": [
                {"label": label, "desired": desired, "observed": observed}
                for label, desired, observed in self.readout_rows
            ],
            "expiry": (
                {
                    "not_after": iso(expiry.not_after),
                    "days_left": expiry.days_left,
                    "renewal_at": iso(expiry.renewal_at),
                }
                if expiry
                else None
            ),
            "awaiting_approval": len(self.awaiting_approval),
            "certificate": (
                {
                    "certificate_name": str(resolved.get("certificate_name", "")),
                    "renewal_window_days": int(resolved.get("renewal_window_days", 0)),
                    "domains": list(resolved.get("domains", ())),
                }
                if resolved is not None
                else None
            ),
            "consumers": [
                {
                    "name": consumer.get("name", ""),
                    "kind": consumer.get("kind", ""),
                    "domains": list(consumer.get("display_domains") or ()),
                    "connection_ref": consumer.get("connection_ref", ""),
                    "url": consumer.get("url", ""),
                }
                for consumer in self.display_consumers
            ],
            "resolution_error": self.resolution_error,
            "used_by": list(self.certificate_use.used_by) if self.certificate_use else [],
            "unused_names": list(self.certificate_use.unused) if self.certificate_use else [],
            "operations": resource_history(resource),
        }


def page_name(resource: ManagedResource) -> str:
    """The name a record's page is titled by: a container's own, the rest as ``record_name`` says it."""

    if resource.kind == CONTAINER_KIND and resource.spec.get("name"):
        return str(resource.spec["name"])
    return record_name(resource.kind, resource.spec, resource.key)


def get_managed_resource(key: str) -> ManagedResourceRecord:
    """One managed record whole, what HQ derives from it, and its operations."""

    try:
        resource = stored(key)
    except ManagedResource.DoesNotExist as exc:
        raise RecordNotFound("That record was not found.") from exc
    return resource_context(resource).record()


def awaiting_approval(key: str) -> tuple[Any, ...]:
    """The changes to the record ``key`` a credential asked for that wait for an operator."""

    return read_once(
        f"infrastructure.awaiting:{key}",
        lambda: tuple(
            held for held in pending(principal=internal_principal("resource-context")) if held.resource_key == key
        ),
    )


def stored(key: str) -> ManagedResource:
    """The record ``key`` as stored, read once for the request that asks."""

    return read_once(f"infrastructure.stored:{key}", lambda: ManagedResource.objects.get(key=key))


def capabilities_of(resource: ManagedResource) -> ResourceCapabilities:
    """What may be done to ``resource``, worked out once for the request that asks."""

    return read_once(f"infrastructure.capabilities:{resource.key}", lambda: resource_capabilities(resource))


def resource_context(resource: ManagedResource) -> ResourceContext:
    """Every fact HQ derives about one resource, derived once."""

    resolved, consumers, error = _consumers(resource)
    return ResourceContext(
        resource=resource,
        capabilities=capabilities_of(resource),
        provider_machine=provider_machine(resource),
        service_links=service_links(resource),
        readout_rows=readout_rows(resource),
        expiry=expiry_of(resource),
        awaiting_approval=awaiting_approval(resource.key),
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

    return certificate_use(resource.key, provider.hostnames(resolved or resource.spec) or ())


@dataclass(frozen=True, slots=True)
class ControllerSummary:
    """What the controller will do for a resource, said once."""

    headline: str
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
    if off and sum(len(names) for names in off.values()) == len(actions):
        return ControllerSummary("HQ does not change this", lines, frozenset(off))
    automatic = any(allowed.automatic for allowed in actions.values())
    return ControllerSummary(
        "Applied automatically" if automatic else f"Applied when you press {OperationRequest.Action.RECONCILE.label}",
        lines,
        frozenset(off),
    )


@dataclass(frozen=True, slots=True)
class RecordGroup:
    """The records of one type, those needing a look first."""

    kind: str
    label: str
    rows: tuple[Any, ...]
    unsettled: int


@dataclass(frozen=True, slots=True)
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

    @property
    def type_choices(self) -> tuple[tuple[str, str], ...]:
        """The filter's choice of type: each type, with how many records it has."""

        return tuple((group.kind, f"{group.label} ({len(group.rows)})") for group in self.types)


def _grouped(rows: list[Any]) -> tuple[RecordGroup, ...]:
    found: dict[str, list[Any]] = {}
    for row in rows:
        found.setdefault(row["kind"], []).append(row)
    groups = []
    for kind, members in found.items():
        # A working record is the only one with nothing to look at.
        unsettled = [row for row in members if row["standing"]["tone"] != Tone.GOOD]
        settled = [row for row in members if row["standing"]["tone"] == Tone.GOOD]
        groups.append(RecordGroup(kind, kind_label(kind), tuple(unsettled + settled), len(unsettled)))
    # A type with something to look at leads; the rest by name.
    return tuple(sorted(groups, key=lambda group: (not group.unsettled, group.label.casefold())))


def record_list(rows: list[ManagedResourceListed], *, query: str = "", kind: str = "") -> RecordList:
    """The list's ``rows`` under their types, kept by the filter: words in a
    row's own text, and one type."""

    types = _grouped(rows)
    wanted = query.strip().casefold()
    kind = kind if any(group.kind == kind for group in types) else ""
    kept = [
        row
        for row in rows
        if (not kind or row["kind"] == kind)
        and (
            not wanted
            or wanted
            in " ".join(
                (row["key"], row["name"], row["summary"], kind_label(row["kind"]), row["standing"]["label"])
            ).casefold()
        )
    ]
    return RecordList(_grouped(kept), types, query.strip(), kind)
