"""What HQ can do with one managed resource, computed from what it knows.

Built from the controller's action policy, the settings gates and the resource
itself, with the same functions the operations enforce. Pages, the topology and
the resource list read this instead of testing kinds.
"""

import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

lazy from django.conf import settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.observations.host import UNIT_KIND
from hq.domains.control_plane.provider_spec import ControllerActionPolicy
from hq.domains.control_plane.providers import (
    PROVIDERS,
    controller_capability_registry,
    resource_home,
)

from .adoption import WriteGap, manages_through, observes_only as observes_through, write_gap
lazy from . import resource_operations
lazy from .capabilities import capability_words
lazy from .facts import inventory_records
lazy from .finding_steps import ByHand, Why
lazy from .machines import connections_inside, container_context
lazy from .pages import PageAction

# Actions a person starts from a resource's page, in the order they lead.
PAGE_VERBS = ("start", "stop", "restart", "reset-failed", "enable", "reconcile", "renew")
LIFECYCLE_VERBS = frozenset({"start", "stop", "restart", "reset-failed", "enable"})
CREATE_CAPABILITY = "infrastructure.resource.create"
# The create's mode on a machine the tailnet's device declaration already names.
MACHINE_DETAILS = "machine_details"
REMOVAL_CAPABILITY = "infrastructure.resource.remove"
UPDATE_CAPABILITY = "infrastructure.resource.update"
# The update's mode on a domain's mail page: a policy composed from its choices.
MAIL_POLICY = "mail_policy"


@dataclass(frozen=True, slots=True, kw_only=True)
class Allowed:
    enabled: bool
    reason: str = ""
    automatic: bool = False


class Offered(NamedTuple):
    """Whether a declaration's edit and its removal are offered.

    Decided once, so every page a declaration is shown on (its own, a
    domain's, a service's, the map's panel) offers the same. Each control is
    its command's (``PageAction.command``, ``command_action``), which says
    its words, address and method and leaves it out for a reader who may not
    run it.
    """

    edits: bool = False
    removes: bool = False

    def edit(self, key: str, **query: str) -> PageAction | None:
        """The control that edits the declaration ``key``, where its edit is offered."""

        return PageAction.command(UPDATE_CAPABILITY, key, query=query or None) if self.edits else None


@dataclass(frozen=True, slots=True)
class ResourceCapabilities:
    # Every verb the controller implements for this kind, except delete.
    actions: Mapping[str, Allowed]
    # "delete" at the provider, "forget" the declaration, or "unavailable".
    removal: str
    removal_reason: str
    observed_at: datetime | None
    unobserved_reason: str
    home: str
    offered: Offered
    removal_pending: bool = False
    # Why its connection cannot make a change, and where that is set up; None when it can.
    write_gap: WriteGap | None = None

    @property
    def removal_variant(self) -> str:
        """Which of the removal command's words its button says: forgetting a
        record HQ only watches is its "forget" variant."""

        return "forget" if self.removal == "forget" else ""

    @property
    def removal_words(self) -> str:
        return capability_words(REMOVAL_CAPABILITY, self.removal_variant)

    @property
    def page_actions(self) -> tuple[tuple[str, Allowed], ...]:
        return tuple((verb, self.actions[verb]) for verb in PAGE_VERBS if verb in self.actions)


def _offered(resource: ManagedResource, provider, removal: str, *, removal_pending: bool) -> Offered:
    # A record its source names now is that source's to write, by hand neither.
    if provider is not None and provider.sourced is not None and provider.sourced(resource):
        return Offered()
    # With public DNS changes off, nothing that would change what the public
    # internet sees is offered; forgetting a declaration leaves it live, so stays.
    held = provider is not None and provider.public_effect and not public_dns_enabled()
    # A withheld kind's form would print its spec; the domain that owns it changes it.
    editable = not removal_pending and not held and not (provider is not None and provider.withheld)
    removable = removal == "forget" or (removal != "unavailable" and not held)
    return Offered(editable, removable)


def offered_changes(resources: Iterable[ManagedResource]) -> dict[str, Offered]:
    """What each declaration offers, by key, in one query however many there are."""

    resources = list(resources)
    if not resources:
        return {}
    pending = removals_pending([resource.pk for resource in resources])
    manages = manages_through()
    registry = controller_capability_registry().capabilities
    offered = {}
    for resource in resources:
        provider = PROVIDERS.get(resource.kind)
        capability = registry.get(resource.kind)
        removal_pending = resource.key in pending
        removal, _reason = _removal(
            provider,
            dict(capability.actions) if capability else {},
            removal_pending,
            observes_through(resource.kind, resource.spec, manages),
        )
        offered[resource.key] = _offered(resource, provider, removal, removal_pending=removal_pending)
    return offered


def kind_converges(kind: str) -> ControllerActionPolicy | None:
    """How the controller applies HQ's config for resources of this kind (its
    ``automatic`` says whether unasked), or None when it applies none."""
    capability = controller_capability_registry().capabilities.get(kind)
    policy = capability.actions.get("reconcile") if capability else None
    return policy if policy is not None and policy.mode == "apply" else None


def public_dns_enabled() -> bool:
    return settings.SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS


def resource_capabilities(
    resource: ManagedResource,
    *,
    running: tuple[str, ...] | None = None,
    removal_pending: bool | None = None,
    manages=None,
) -> ResourceCapabilities:
    """``running`` is the lifecycle verbs the resource's state allows; left as
    None it is read from the latest sweep, and () leaves lifecycle verbs out.
    ``removal_pending`` left as None is read from the resource's operations.
    ``manages`` is an ``adoption.manages_through()`` test, shared by a caller
    asking about many resources; left as None it is built here."""
    provider = PROVIDERS.get(resource.kind)
    if removal_pending is None:
        removal_pending = resource.key in removals_pending((resource.pk,))
    capability = controller_capability_registry().capabilities.get(resource.kind)
    policies = dict(capability.actions) if capability else {}
    if running is None:
        running = _running_verbs(resource) if LIFECYCLE_VERBS & policies.keys() else ()

    # A kind that acts through a connection acts only through one that manages.
    gap = write_gap(resource.kind, resource.spec, manages)

    actions = {
        verb: _allowed(verb, policy, resource, provider, removal_pending, gap)
        for verb, policy in policies.items()
        if verb != "delete" and policy.mode == "apply" and (verb not in LIFECYCLE_VERBS or verb in running)
    }
    _hold_out_of_reach_units(resource, actions)
    removal, removal_reason = _removal(provider, policies, removal_pending, gap is not None)

    return ResourceCapabilities(
        actions=actions,
        removal=removal,
        removal_reason=removal_reason,
        observed_at=resource.last_observed_at,
        unobserved_reason=provider.unobserved_reason if provider is not None else "",
        home=resource_home(resource),
        removal_pending=removal_pending,
        offered=_offered(resource, provider, removal, removal_pending=removal_pending),
        write_gap=gap,
    )


def declaration_actions(key: str) -> tuple[PageAction, ...]:
    """Edit and removal of the declaration ``key``, as the page of what it
    declares (a machine, a domain) offers them; none when HQ keeps none."""

    resource = ManagedResource.objects.filter(key=key).first() if key else None
    if resource is None:
        return ()
    capabilities = resource_capabilities(resource)
    offered = capabilities.offered
    shown = (
        offered.edit(key),
        PageAction.command(REMOVAL_CAPABILITY, key, variant=capabilities.removal_variant) if offered.removes else None,
    )
    return tuple(filter(None, shown))


def _allowed(verb, policy, resource, provider, removal_pending, gap: WriteGap | None) -> Allowed:
    if removal_pending:
        return Allowed(enabled=False, reason="A removal is in progress.", automatic=policy.automatic)
    if not resource.enabled:
        return Allowed(enabled=False, reason="It is switched off in HQ.", automatic=policy.automatic)
    if gap is not None:
        return Allowed(enabled=False, reason=gap.reason, automatic=policy.automatic)
    if verb == "reconcile" and provider is not None and provider.public_effect and not public_dns_enabled():
        return Allowed(enabled=False, reason="Public DNS changes are off on this server.")
    if verb == "renew":
        renewable, why = resource_operations.certificate_renewal_allowed(resource)
        return Allowed(enabled=renewable, reason=why, automatic=policy.automatic)
    return Allowed(enabled=True, automatic=policy.automatic)


def _hold_out_of_reach_units(resource: ManagedResource, actions: dict[str, Allowed]) -> None:
    """A lifecycle verb on a container its unit runs is asked of the unit,
    which HQ reaches only on its own machine; elsewhere it is held with why."""

    offered = LIFECYCLE_VERBS & actions.keys()
    if not offered or not (gap := resource_operations.unit_gap(resource)):
        return
    for verb in offered:
        if actions[verb].enabled:
            actions[verb] = Allowed(enabled=False, reason=gap, automatic=actions[verb].automatic)


def _removal(provider, policies, removal_pending, observes_only) -> tuple[str, str]:
    if removal_pending:
        return "unavailable", "A removal is in progress."
    if provider is not None and provider.declaration_only:
        return "forget", ""
    if observes_only:
        # HQ can still stop managing it; it cannot delete it at the provider.
        return "forget", ""
    delete = policies.get("delete")
    if delete and delete.mode == "apply":
        return "delete", ""
    return "unavailable", (
        (provider.removal_gap if provider is not None else "")
        or (delete.reason if delete else "")
        or "HQ cannot remove this."
    )


def removals_pending(resource_ids=None) -> frozenset[str]:
    """Keys of resources with a deletion queued or claimed, in one query."""

    pending = OperationRequest.objects.filter(
        action=OperationRequest.Action.DELETE,
        state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED),
    )
    if resource_ids is not None:
        pending = pending.filter(resource_id__in=resource_ids)
    return frozenset(pending.values_list("resource__key", flat=True))


def _running_verbs(resource: ManagedResource) -> tuple[str, ...]:
    spec = resource.spec or {}
    if resource.kind == UNIT_KIND:
        return next(
            (
                _unit_verbs(record)
                for _, record in inventory_records(UNIT_KIND)
                if record.get("unit") == spec.get("unit")
            ),
            (),
        )
    running = (container_context(spec.get("host", ""), spec.get("name", "")) or {}).get("running")
    return tuple(running.verbs) if running else ()


def _unit_verbs(record: Mapping[str, object]) -> tuple[str, ...]:
    """The lifecycle verbs a unit's state, as last read, allows."""

    active = record.get("active")
    return (
        *(("start",) if active in ("inactive", "failed") else ()),
        *(("restart",) if active == "active" else ()),
        *(("reset-failed",) if active == "failed" else ()),
        *(("enable",) if record.get("file_state") == "disabled" else ()),
    )


@dataclass(frozen=True, slots=True)
class ConnectionInside:
    """A connection HQ reaches machines through that runs inside one container.

    Stopping that container cuts the connection, and when the container's own
    verbs travel through it (``carries_its_verbs``), HQ cannot start it again.
    """

    connection_ref: str
    machines: tuple[str, ...]
    carries_its_verbs: bool
    # How the owner starts it again on its machine; None when no command can
    # be written for that machine's name.
    start: ByHand | None


def channels_inside(resource: ManagedResource) -> tuple[ConnectionInside, ...]:
    """The connections whose endpoint is this container (``machines.connections_inside``)."""

    spec = resource.spec or {}
    host = str(spec.get("host", "")).strip()
    name = str(spec.get("name", "")).strip()
    if not host or not name:
        return ()
    provider = PROVIDERS.get(resource.kind)
    acts_through = provider.connection_providers if provider is not None else ()
    own_ref = str(spec.get("connection_ref", "")).strip()
    # A container its unit runs is started by starting the unit, which also
    # starts what the unit runs first.
    supervised = resource_operations.supervision(resource)
    start = next(
        iter(
            ByHand.on(
                host,
                f"Start it again on {host}",
                f"systemctl start {shlex.quote(supervised.unit)}"
                if supervised is not None
                else f"docker start {shlex.quote(name)}",
                Why.CHANNEL,
            )
        ),
        None,
    )
    inside = (rows for (on, named), rows in connections_inside().items() if (on.lower(), named) == (host.lower(), name))
    return tuple(
        ConnectionInside(
            connection_ref=row.connection_ref,
            machines=tuple(dict.fromkeys(str(item) for item in row.reaches or () if item)),
            carries_its_verbs=row.provider in acts_through and own_ref in ("", row.connection_ref),
            start=start,
        )
        for rows in inside
        for row in rows
    )
