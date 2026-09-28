"""Typed provider declarations: emit once, derive every adapter contract."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from django.urls import reverse

from pydantic import (
    TypeAdapter,
)


from .attribution import unattributed_kinds
from .connection_kinds import CONNECTION_CREDENTIALS
from .observations import OBSERVATIONS
from .provider_adapters import DECLARATIONS
from .provider_spec import (
    SERVICE_FACETS,
    ControllerCapabilityRegistry,
    ControllerProviderCapability,
    ProviderResolutionContext,
    ProviderSpec,
)


def service_facets() -> tuple[tuple[str, str], ...]:
    """The facets to render, in catalogue order.

    A facet nothing supplies is a gap in HQ, not in the service, and a column
    with nothing in it tells the operator to go fix something they cannot. So a
    facet may be declared ahead of the provider that fills it and stays
    invisible until that provider is registered.
    """

    supplyable = {provider.facet for provider in PROVIDERS.values() if provider.facet}
    return tuple(
        (facet, label) for facet, label in SERVICE_FACETS if facet in supplyable
    )


    # No operating system field: the tailnet reports `os` for every device.


def registry_label(kind: str) -> str:
    """What a registered resource or reading kind is called. Never the identifier."""

    provider = PROVIDERS.get(kind)
    if provider is not None and provider.label:
        return provider.label
    if kind in OBSERVATIONS:
        return OBSERVATIONS[kind].label
    return "Unregistered kind"


def _registry() -> dict[str, ProviderSpec]:
    registry: dict[str, ProviderSpec] = {}
    for definition in DECLARATIONS:
        if definition.kind in registry:
            raise ValueError(f"Two provider modules declare {definition.kind!r}.")
        registry[definition.kind] = definition
    return registry


# Every kind any admitted module declares, in admission order. Derived, so a
# provider joins by being admitted and nothing here names it.
PROVIDERS = _registry()


def resource_home(resource: Any) -> str:
    """The URL of the page a resource lives on."""
    provider = PROVIDERS.get(resource.kind)
    if provider is not None and provider.home is not None:
        return provider.home(resource)
    return reverse("control_plane:detail", kwargs={"key": resource.key})


def readout_rows(resource: Any) -> tuple[tuple[str, str, str], ...]:
    """``(label, desired, observed)`` as the resource's provider describes it."""

    provider = PROVIDERS.get(resource.kind)
    if provider is None or provider.readout is None:
        return ()
    try:
        return tuple(provider.readout(resource.spec, resource.status or {}))
    except (KeyError, TypeError, ValueError):
        return ()


# Kinds a controller reports as readings rather than as resources; see
# control_plane.observations.
OBSERVATION_KINDS = frozenset(OBSERVATIONS)


@dataclass(frozen=True)
class ObserverAbility:
    """What a connection is carried for when it reconciles nothing.

    Every other ability is derived from a resource kind: a credential exists to
    make some declaration true, so the kind is the record of why it is held.
    A connection that only ever reads has no kind to derive from, and left at
    that it appears on the connections page holding no authority at all, which
    reads as a credential nobody can account for rather than as a reader.

    So a reader declares its ability here, against the resource it answers for.
    The effect is always a read: if something wants to change state it needs a
    kind, and a kind is what the reconcile machinery keys on.
    """

    provider: str
    name: str
    label: str
    summary: str
    subject_resource: str


_OBSERVER_ABILITIES: tuple[ObserverAbility, ...] = (
    ObserverAbility(
        provider="cloudflare_api",
        name="analytics.read",
        label="Site analytics",
        summary=(
            "Reads site traffic (pages, referrers, countries, devices, "
            "browsers, operating systems) and Core Web Vitals."
        ),
        subject_resource="analytics",
    ),
)


def observer_abilities() -> tuple[ObserverAbility, ...]:
    return _OBSERVER_ABILITIES


# A provider a resource can be reconciled through, or an observer can read
# through, without a credential model would be reported as "proof undeclared"
# forever. Refused here, beside the declarations, rather than found on the page.
_unmodelled = sorted(
    (
        {provider for spec in PROVIDERS.values() for provider in spec.connection_providers}
        | {ability.provider for ability in _OBSERVER_ABILITIES}
    )
    - set(CONNECTION_CREDENTIALS)
)
if _unmodelled:
    raise ValueError(
        "Connection providers without a credential model: "
        f"{', '.join(_unmodelled)}."
    )


if _unattributed := unattributed_kinds(OBSERVATIONS, PROVIDERS.values()):
    raise ValueError(
        "Kinds read through a per-connection provider must name connection_ref: "
        f"{', '.join(_unattributed)}."
    )


@lru_cache(maxsize=1)
def controller_capability_registry() -> ControllerCapabilityRegistry:
    """What the controller may do, assembled from the providers themselves.

    A provider knows whether the controller can converge it, whether it should
    do so unprompted, and why not when not; those are properties of the thing,
    not of a deployment.
    """

    missing = sorted(
        kind for kind, provider in PROVIDERS.items() if not provider.actions
    )
    if missing:
        raise ValueError(
            "Every provider must declare what the controller may do to it. "
            "Missing: " + ", ".join(missing)
        )
    return ControllerCapabilityRegistry(
        schema_version=1,
        capabilities={
            kind: ControllerProviderCapability(actions=dict(provider.actions))
            for kind, provider in PROVIDERS.items()
        },
    )


def controller_id() -> str:
    """Which controller this deployment runs.

    An identity, not a policy: it names one installation, so it arrives from the
    environment rather than from the committed contract beside it.

    Falling back to the machine's own name rather than to a word. This is what
    a sweep files its findings under, so a placeholder would put every container
    on a host called "controller", and both processes that ask run on the host
    network, so both get the same answer without anything being passed between
    them.
    """

    return os.environ.get("HQ_CONTROLLER_ID", "").strip() or os.uname().nodename


def controller_capabilities() -> dict[str, Any]:
    """Return the one validated, JSON-safe controller contract."""

    contract = controller_capability_registry().model_dump(mode="json")
    contract["controller_id"] = controller_id()
    return contract


def enabled_controller_actions(
    *, automatic_only: bool = False
) -> tuple[tuple[str, str], ...]:
    registry = controller_capability_registry()
    return tuple(
        sorted(
            (kind, action)
            for kind, capability in registry.capabilities.items()
            for action, policy in capability.actions.items()
            if policy.mode == "apply" and (policy.automatic or not automatic_only)
        )
    )


def controller_action_policy(kind: str, action: str) -> tuple[bool, str]:
    capability = controller_capability_registry().capabilities.get(kind)
    policy = capability.actions.get(action) if capability else None
    if not policy:
        return False, f"The controller does not implement {action!r} for {kind!r}."
    if policy.mode != "apply":
        return False, policy.reason or "The controller cannot run this action."
    return True, "The controller can run this action."


def describe_providers() -> dict[str, Any]:
    capabilities = controller_capabilities()["capabilities"]
    return {
        "schema_version": 1,
        "controller": {
            "id": controller_capabilities()["controller_id"],
            "capabilities": capabilities,
        },
        "providers": [
            {
                "kind": provider.kind,
                "label": provider.label or provider.kind,
                "summary": provider.summary,
                "destructive": provider.destructive,
                "public_effect": provider.public_effect,
                # Said out loud in the contract, so a caller can know before it
                # calls that this kind waits for a person, rather than
                # discovering it from the answer to a change it has already
                # asked for.
                "requires_approval": provider.requires_approval,
                # Part of the contract, not a detail of one page: anything that
                # offers "add a resource" has to know which kinds stand on their
                # own and which only make sense inside something else.
                "created_from": provider.created_from,
                "controller": capabilities[provider.kind],
                "spec_schema": provider.schema(),
            }
            for provider in PROVIDERS.values()
        ],
    }


def validate_spec(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        provider = PROVIDERS[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown infrastructure resource kind {kind!r}.") from exc
    validated = provider.validate(payload)
    dumped: dict[str, Any] = TypeAdapter(provider.spec_type).dump_python(validated, mode="json")
    return dumped


def resolve_provider_spec(
    kind: str,
    payload: dict[str, Any],
    *,
    context: ProviderResolutionContext,
) -> dict[str, Any]:
    """Validate authored state, resolve references, then validate runtime state."""

    provider = PROVIDERS[kind]
    authored = validate_spec(kind, payload)
    resolved = provider.resolver(authored, context) if provider.resolver else authored
    resolved_type = provider.resolved_type or provider.spec_type
    value: Any = TypeAdapter(resolved_type).validate_python(resolved)
    dumped: dict[str, Any] = TypeAdapter(resolved_type).dump_python(value, mode="json")
    return dumped
