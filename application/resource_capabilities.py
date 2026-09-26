"""What HQ can do with one managed resource, computed from what it knows.

Built from the controller's action policy, the settings gates and the resource
itself, with the same functions the operations enforce. Pages, the topology and
the resource list read this instead of testing kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from control_plane.models import ManagedResource

from .adoption import OBSERVES_ONLY
from control_plane.providers import (
    PROVIDERS,
    controller_capability_registry,
    resource_home,
)

# Actions a person starts from a resource's page, in the order they lead.
PAGE_VERBS = ("start", "stop", "restart", "reconcile", "renew")
VERB_LABELS = {
    "start": "Start",
    "stop": "Stop",
    "restart": "Restart",
    "reconcile": "Reconcile",
    "renew": "Request renewal",
    "approve-routes": "Approve routes",
}
LIFECYCLE_VERBS = frozenset({"start", "stop", "restart"})


@dataclass(frozen=True)
class Allowed:
    enabled: bool
    reason: str = ""
    automatic: bool = False


@dataclass(frozen=True)
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
    from django.conf import settings

    return bool(getattr(settings, "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False))


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
        removal_pending = resource.pk is not None and resource.key in removals_pending(
            (resource.pk,)
        )
    capability = controller_capability_registry().capabilities.get(resource.kind)
    policies = dict(capability.actions) if capability else {}
    if running is None:
        running = _running_verbs(resource) if LIFECYCLE_VERBS & policies.keys() else ()

    # A kind that acts through a connection acts only through one that manages.
    from .adoption import observes_only as observes_through

    observes_only = observes_through(resource.kind, resource.spec, manages)

    actions = {
        verb: _allowed(verb, policy, resource, provider, removal_pending, observes_only)
        for verb, policy in policies.items()
        if verb != "delete"
        and policy.mode == "apply"
        and (verb not in LIFECYCLE_VERBS or verb in running)
    }
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
    from .infrastructure import certificate_renewal_allowed

    if removal_pending:
        return Allowed(False, "A removal is in progress.", policy.automatic)
    if not resource.enabled:
        return Allowed(False, "This resource is disabled.", policy.automatic)
    if observes_only:
        return Allowed(False, OBSERVES_ONLY, policy.automatic)
    if (
        verb == "reconcile"
        and provider is not None
        and provider.public_effect
        and not public_dns_enabled()
    ):
        return Allowed(False, "Public DNS changes are off on this server.")
    if verb == "renew":
        renewable, why = certificate_renewal_allowed(resource)
        return Allowed(renewable, why, policy.automatic)
    return Allowed(True, automatic=policy.automatic)


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
        or "The controller cannot delete this kind."
    )


def removals_pending(resource_ids=None) -> frozenset[str]:
    """Keys of resources with a deletion queued or claimed, in one query."""
    from control_plane.models import OperationRequest

    pending = OperationRequest.objects.filter(
        action=OperationRequest.Action.DELETE,
        state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED),
    )
    if resource_ids is not None:
        pending = pending.filter(resource_id__in=resource_ids)
    return frozenset(pending.values_list("resource__key", flat=True))


def _running_verbs(resource: ManagedResource) -> tuple[str, ...]:
    from .machines import container_context

    spec = resource.spec or {}
    running = (container_context(spec.get("host", ""), spec.get("name", "")) or {}).get(
        "running"
    )
    return tuple(running.verbs) if running else ()
