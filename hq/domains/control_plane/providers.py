"""Typed provider declarations: emit once, derive every adapter contract."""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache, lru_cache
from typing import Any, Literal

from hq.platform.application.labels import human_label
from hq.platform.application.record_errors import Sentence
lazy from hq.platform.application.default_hold import needs_approval
lazy from hq.platform.application.entity_links import page_url
lazy from hq.platform.application.readings import KEPT
lazy from hq.platform.application.references import Kind

from .attribution import logging_in, unattributed_kinds
from .connection_kinds import CONNECTION_LABELS
from .observations import OBSERVATIONS
from .observations.contract import Reader, ReadingPart
from .provider_adapters import DECLARATIONS
from .provider_spec import (
    SERVICE_FACETS,
    ControllerCapabilityRegistry,
    ControllerProviderCapability,
    ProviderResolutionContext,
    ProviderSpec,
    adapter,
)

logger = logging.getLogger("severino.providers")


def service_facets() -> tuple[tuple[str, str], ...]:
    """The facets to render, in catalog order.

    A facet nothing supplies is a gap in HQ, not in the service, and a column
    with nothing in it tells the operator to go fix something they cannot. So a
    facet may be declared ahead of the provider that fills it and stays
    invisible until that provider is registered. A reading fills a facet as a
    declaration does, so either supplies it.
    """

    supplyable = {
        *(provider.facet for provider in PROVIDERS.values() if provider.facet),
        *(spec.facet for spec in OBSERVATIONS.values() if spec.facet),
    }
    return tuple((facet, label) for facet, label in SERVICE_FACETS if facet in supplyable)

    # No operating system field: the tailnet reports `os` for every device.


def registry_label(kind: str) -> str:
    """What a resource or reading kind is called: its declared label, else,
    for a stored kind nothing declares any more, its identifier in words."""

    if kind in PROVIDERS:
        return PROVIDERS[kind].label
    if kind in OBSERVATIONS:
        return OBSERVATIONS[kind].label
    return human_label(kind)


def operator_label(kind: str) -> str:
    """Who answers for a kind's readings, named as the connection they are
    read through; the kind's label where no connection names it, and "" for a
    kind nothing declares."""

    spec = PROVIDERS.get(kind)
    if spec is None:
        return ""
    return CONNECTION_LABELS[spec.connection_providers[0]] if spec.connection_providers else spec.label


def _registry() -> dict[str, ProviderSpec]:
    registry: dict[str, ProviderSpec] = {}
    for definition in DECLARATIONS:
        if definition.kind in registry:
            raise ValueError(f"Two modules name {definition.kind!r}.")
        registry[definition.kind] = definition
    return registry


# Every kind any admitted module declares, in admission order. Derived, so a
# provider joins by being admitted and nothing here names it.
PROVIDERS = _registry()


def observed(kind: str) -> bool:
    """Whether anything reads ``kind``: it is a reading, or a declaration a
    sweep reads back (``ProviderSpec.sweeps``). A kind nothing declares is not."""

    spec = PROVIDERS.get(kind)
    return kind in OBSERVATIONS or (spec is not None and spec.sweeps)


def unswept(kind: str) -> bool:
    """Whether HQ declares ``kind`` and nothing sweeps it, so it can never be
    behind a sweep. A kind HQ does not declare is read by whatever brings it."""

    spec = PROVIDERS.get(kind)
    return spec is not None and not spec.sweeps


def resource_home(resource: Any) -> str:
    """The URL of the page a resource lives on."""
    provider = PROVIDERS.get(resource.kind)
    if provider is not None and provider.home is not None:
        return provider.home(resource)
    return page_url(Kind.RESOURCE, resource.key)


def spec_withheld(kind: str) -> bool:
    """Whether generic surfaces must not show a declaration's spec (``ProviderSpec.withheld``)."""

    provider = PROVIDERS.get(kind)
    return provider is not None and provider.withheld


def shown_spec(kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """A spec as a generic surface may show it: whole, or nothing of a withheld kind."""

    return {} if spec_withheld(kind) else spec


def readout_rows(resource: Any) -> tuple[tuple[str, str, str], ...]:
    """``(label, desired, observed)`` as the resource's provider describes it."""

    provider = PROVIDERS.get(resource.kind)
    if provider is None or provider.readout is None:
        return ()
    try:
        return tuple(provider.readout(resource.spec, resource.status or {}))
    except KeyError, TypeError, ValueError:
        return ()


def read_record[T](kind: str, record: Mapping[str, Any], read: Callable[[dict[str, Any]], T]) -> T | None:
    """What ``read`` makes of the spec a swept record reproduces (``read_spec``),
    or None when its provider cannot read the record."""

    spec = record_spec(kind, record)
    return None if spec is None else read_spec(kind, spec, read)


def record_spec(kind: str, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The spec a swept record reproduces (``ProviderSpec.record_spec``), or None."""

    provider = PROVIDERS.get(kind)
    return None if provider is None else provider.record_spec(record)


def read_spec[T](kind: str, spec: Mapping[str, Any], read: Callable[[dict[str, Any]], T]) -> T | None:
    """What ``read`` makes of a declared spec through its provider's hooks, or
    None when they cannot read it.

    The one place a hook's failure on a declaration is caught. validate_spec
    holds every spec on write, so a spec its own provider cannot read is a
    defect in one of the two, and it is logged; leaving the declaration out of
    a page hides it from everything that reads it.
    """

    try:
        return read(dict(spec))
    except (KeyError, TypeError, ValueError) as error:
        logger.warning(
            "A %s spec is one its provider cannot read: %r",
            kind,
            error,
            extra={"event": "providers.spec.unreadable", "kind": kind},
        )
        return None


@dataclass(frozen=True, slots=True)
class Read:
    """One thing a provider reads: a reading, a resource kind a sweep reads
    back, the parts of another provider's kind, or a value kept outside the
    inventory (``readings.Kept``)."""

    kind: str
    label: str
    source: Literal["reading", "resource", "part"]
    by: Reader = "controller"
    permissions: tuple[str, ...] = ()
    # The kept value this is, whose ``through`` says whether a stored value
    # came through this provider; ``kind`` is then the cadence it is judged by.
    kept: Any = None


@cache
def provider_reads() -> Mapping[str, tuple[Read, ...]]:
    """Everything each provider reads, by provider: the one index of what
    reads what, from the declarations alone."""

    found: dict[str, list[Read]] = {}
    for kind, reading in OBSERVATIONS.items():
        found.setdefault(reading.provider, []).append(
            Read(kind, reading.label, "reading", reading.read_by, tuple(reading.requires))
        )
    for kind, spec in PROVIDERS.items():
        if spec.sweeps:
            for provider in spec.connection_providers:
                found.setdefault(provider, []).append(Read(kind, spec.label, "resource"))
        by_provider: dict[str, list[ReadingPart]] = {}
        for part in spec.parts:
            if part.provider and part.provider not in spec.connection_providers:
                by_provider.setdefault(part.provider, []).append(part)
        for provider, parts in by_provider.items():
            label = ", ".join(part.label for part in parts)
            needs = tuple(dict.fromkeys(name for part in parts for name in part.requires))
            found.setdefault(provider, []).append(Read(kind, label, "part", permissions=needs))
    for kept in KEPT:
        for provider in kept.through:
            found.setdefault(provider, []).append(Read(kept.cadence, kept.label, "reading", "hq", kept=kept))
    return frozendict({provider: tuple(reads) for provider, reads in found.items()})


# Kinds a controller reports as readings rather than as resources; see
# control_plane.observations.
OBSERVATION_KINDS = frozenset(OBSERVATIONS)
# Every kind read by logging in to a machine (``attribution.logging_in``).
LOGGING_IN = logging_in(OBSERVATIONS, PROVIDERS.values())


if _unattributed := unattributed_kinds(OBSERVATIONS, PROVIDERS.values()):
    raise ValueError(
        f"Types read through a connection of their own must name connection_ref: {', '.join(_unattributed)}."
    )


@lru_cache(maxsize=1)
def controller_capability_registry() -> ControllerCapabilityRegistry:
    """What the controller may do, assembled from the providers themselves.

    A provider knows whether the controller can converge it, whether it should
    do so unprompted, and why not when not; those are properties of the thing,
    not of a deployment.
    """

    missing = sorted(kind for kind, provider in PROVIDERS.items() if not provider.actions)
    if missing:
        raise ValueError("Every connection type must say what HQ may do to it. Missing: " + ", ".join(missing))
    return ControllerCapabilityRegistry(
        schema_version=1,
        capabilities={
            kind: ControllerProviderCapability(actions=dict(provider.actions)) for kind, provider in PROVIDERS.items()
        },
    )


def controller_id() -> str:
    """Which controller this deployment runs.

    An identity of one installation, so it arrives from the environment rather than
    the committed contract. A sweep files its findings under it, so it falls back
    to the machine's own name, which both processes that ask share on the host
    network, where a placeholder would put every container on a host called
    "controller".
    """

    return os.environ.get("HQ_CONTROLLER_ID", "").strip() or os.uname().nodename


def controller_capabilities() -> dict[str, Any]:
    """Return the one validated, JSON-safe controller contract."""

    contract = controller_capability_registry().model_dump(mode="json")
    contract["controller_id"] = controller_id()
    return contract


def enabled_controller_actions(*, automatic_only: bool = False) -> tuple[tuple[str, str], ...]:
    registry = controller_capability_registry()
    return tuple(
        sorted(
            (kind, action)
            for kind, capability in registry.capabilities.items()
            for action, policy in capability.actions.items()
            if policy.mode == "apply" and (policy.automatic or not automatic_only)
        )
    )


def controller_action_policy(kind: str, action: str) -> tuple[bool, Sentence]:
    """Whether the controller may run ``action`` on a ``kind``, and why, as a refusal may say it."""

    capability = controller_capability_registry().capabilities.get(kind)
    policy = capability.actions.get(action) if capability else None
    if not policy:
        return False, "HQ does not do that to this type of record."
    if policy.mode != "apply":
        return False, policy.why_locked()
    return True, "HQ can run this action."


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
                "label": provider.label,
                "summary": provider.summary,
                "destructive": provider.destructive,
                "public_effect": provider.public_effect,
                # Said out loud in the contract, so a caller can know before it
                # calls that this kind waits for a person, rather than
                # discovering it from the answer to a change it has already
                # asked for.
                "requires_approval": needs_approval(provider),
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
        raise ValueError(f"Unknown record type {kind!r}.") from exc
    validated = provider.validate(payload)
    dumped: dict[str, Any] = adapter(provider.spec_type).dump_python(validated, mode="json")
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
    value: Any = adapter(resolved_type).validate_python(resolved)
    dumped: dict[str, Any] = adapter(resolved_type).dump_python(value, mode="json")
    return dumped
