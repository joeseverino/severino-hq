"""What HQ can do with one managed resource, computed from what it knows.

Built from the controller's action policy, the settings gates and the resource
itself, with the same functions the operations enforce. Pages, the topology and
the resource list read this instead of testing kinds.
"""

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

lazy from django.conf import settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.providers import (
    PROVIDERS,
    controller_capability_registry,
    resource_home,
)

from .adoption import OBSERVES_ONLY, observes_only as observes_through
lazy from . import resource_operations
lazy from .capabilities import capability_words
lazy from .connections import connection_rows
lazy from .finding_model import on_machine
lazy from .machines import container_context
lazy from .whereabouts import locate, whereabouts

# Actions a person starts from a resource's page, in the order they lead.
PAGE_VERBS = ("start", "stop", "restart", "reconcile", "renew")
LIFECYCLE_VERBS = frozenset({"start", "stop", "restart"})
REMOVAL_CAPABILITY = "infrastructure.resource.remove"
UPDATE_CAPABILITY = "infrastructure.resource.update"


def verb_words(verb: str) -> str:
    """What a resource's verb is called: the words its operation declares."""

    return OperationRequest.Action(verb).label


@dataclass(frozen=True, slots=True, kw_only=True)
class Allowed:
    enabled: bool
    reason: str = ""
    automatic: bool = False


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
    removal_pending: bool = False

    @property
    def removal_words(self) -> str:
        """What the removal button says: forgetting a record HQ only watches
        is the removal command's own "forget" words."""

        return capability_words(REMOVAL_CAPABILITY, "forget" if self.removal == "forget" else "")

    @property
    def converges(self) -> bool:
        return "reconcile" in self.actions

    @property
    def page_actions(self) -> tuple[tuple[str, Allowed], ...]:
        return tuple((verb, self.actions[verb]) for verb in PAGE_VERBS if verb in self.actions)


def kind_converges(kind: str) -> bool:
    """Whether the controller reconciles resources of this kind."""
    capability = controller_capability_registry().capabilities.get(kind)
    policy = capability.actions.get("reconcile") if capability else None
    return bool(policy and policy.mode == "apply")


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
    observes_only = observes_through(resource.kind, resource.spec, manages)

    actions = {
        verb: _allowed(verb, policy, resource, provider, removal_pending, observes_only)
        for verb, policy in policies.items()
        if verb != "delete" and policy.mode == "apply" and (verb not in LIFECYCLE_VERBS or verb in running)
    }
    _hold_out_of_reach_units(resource, actions)
    removal, removal_reason = _removal(provider, policies, removal_pending, observes_only)

    return ResourceCapabilities(
        actions=actions,
        removal=removal,
        removal_reason=removal_reason,
        observed_at=resource.last_observed_at,
        unobserved_reason=provider.unobserved_reason if provider is not None else "",
        home=resource_home(resource),
        removal_pending=removal_pending,
    )


def _allowed(verb, policy, resource, provider, removal_pending, observes_only) -> Allowed:
    if removal_pending:
        return Allowed(enabled=False, reason="A removal is in progress.", automatic=policy.automatic)
    if not resource.enabled:
        return Allowed(enabled=False, reason="It is switched off in HQ.", automatic=policy.automatic)
    if observes_only:
        return Allowed(enabled=False, reason=OBSERVES_ONLY, automatic=policy.automatic)
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
        or "The controller cannot remove this."
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
    running = (container_context(spec.get("host", ""), spec.get("name", "")) or {}).get("running")
    return tuple(running.verbs) if running else ()


@dataclass(frozen=True, slots=True)
class ConnectionInside:
    """A connection HQ reaches machines through that runs inside one container.

    Stopping that container cuts the connection, and when the container's own
    verbs travel through it (``carries_its_verbs``), HQ cannot start it again.
    """

    connection_ref: str
    machines: tuple[str, ...]
    carries_its_verbs: bool
    # How the owner starts it again on its machine; "" when no command can be
    # written for that machine's name.
    start_command: str


def channels_inside(resource: ManagedResource) -> tuple[ConnectionInside, ...]:
    """The connections whose endpoint is this container, placed the way every
    address HQ holds is placed (``whereabouts.locate``)."""

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
    start_command = on_machine(
        host,
        f"sudo systemctl start {shlex.quote(supervised.unit)}"
        if supervised is not None
        else f"docker start {shlex.quote(name)}",
    )
    at = whereabouts()
    found: dict[str, ConnectionInside] = {}
    for row in connection_rows():
        if not row.endpoint or row.connection_ref in found:
            continue
        # The endpoint is as the controller reading it was given it, so a
        # loopback one is the controller's own machine.
        origin = locate(row.endpoint, at=at, loopback_on=row.controller_id)
        if origin.host.lower() != host.lower() or origin.container != name:
            continue
        found[row.connection_ref] = ConnectionInside(
            connection_ref=row.connection_ref,
            machines=tuple(dict.fromkeys(str(item) for item in row.reaches or () if item)),
            carries_its_verbs=row.provider in acts_through and own_ref in ("", row.connection_ref),
            start_command=start_command,
        )
    return tuple(found.values())
