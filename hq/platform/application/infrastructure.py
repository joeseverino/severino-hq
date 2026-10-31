"""Infrastructure desired state and policy-gated operation requests."""

from dataclasses import dataclass
from typing import Annotated, Any

from django.conf import settings
from django.db import transaction
lazy from django.utils.text import slugify

from hq.domains.control_plane.desired_state import advance_dependents, desired_fingerprint
from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.caddy import CADDY_ROUTE_KIND
from hq.domains.control_plane.provider_adapters.declarations import (
    DELIVERY_TARGET_KIND,
    MACHINE_KIND,
)
from hq.domains.control_plane.providers import (
    PROVIDERS,
    enabled_controller_actions,
    registry_label,
    resolve_provider_spec,
    shown_spec,
    spec_withheld,
    validate_spec,
)
from hq.platform.core.audit import operation_context
lazy from hq.domains.control_plane.provider_spec import ProviderResolutionContext

from .adoption import OBSERVES_ONLY, observes_only
from .approvals import consent_gap
from .command_fields import Choices, Describe
from .projection import matched, page_size, read_once
from .record_errors import PolicyError
from .record_saves import Held, held
from .security import Principal
lazy from .delivery_progress import deploying_label
lazy from .locate import names_by_connection
lazy from .resource_capabilities import kind_converges
lazy from .resource_operations import queue_reconcile


@dataclass(frozen=True, slots=True)
class ManagedResourceCommand:
    key: Annotated[str, Describe("The key the record is stored under.")]
    kind: Annotated[
        str,
        # The registry's kinds, the only ones validate_spec accepts.
        Choices((kind, registry_label(kind)) for kind in PROVIDERS),
        Describe("What type of record it is."),
    ]
    spec: Annotated[dict[str, Any], Describe("What HQ expects of it, in the shape its type takes.")]
    enabled: Annotated[bool, Describe("Whether HQ keeps the live record as it expects.")] = True


def list_managed_resources(*, limit: int = 50, kind: str | None = None, kinds: str | None = None) -> dict[str, Any]:
    """List canonical public infrastructure state without provider credentials."""
    # The shared bound, so this module follows it when it moves.
    resources = ManagedResource.objects.all()
    if kind and kinds:
        raise ValueError("Choose either kind or kinds, not both.")
    if kind:
        resources = resources.filter(kind=kind)
    elif kinds:
        resources = resources.filter(kind__in=kinds.split(","))
    page = list(resources[: page_size(limit)])
    items = [serialize_resource(resource) for resource in page]
    return {"items": items, "count": len(items), "total": matched(resources, page, limit)}


def enabled_resources() -> tuple[ManagedResource, ...]:
    """Every enabled declaration, read once per projection and shared."""

    return read_once(
        "infrastructure.enabled_resources",
        lambda: tuple(ManagedResource.objects.filter(enabled=True)),
    )


def _declared(*kinds: str) -> dict[str, tuple[dict[str, Any], ...]]:
    """The specs of several declaration kinds, in one read.

    Read once and passed down rather than looked up per resource: a projection
    resolving fifty declarations wants one query, not fifty, and asking for
    two kinds separately is two queries for one page's worth of context.
    """

    found: dict[str, list[dict[str, Any]]] = {kind: [] for kind in kinds}
    for resource in enabled_resources():
        if resource.kind in found:
            found[resource.kind].append(resource.spec)
    return {kind: tuple(specs) for kind, specs in found.items()}


def context_for_resolution() -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """``(machines, delivery targets)``: everything resolution reads, once."""

    declared = _declared(MACHINE_KIND, DELIVERY_TARGET_KIND)
    return declared[MACHINE_KIND], declared[DELIVERY_TARGET_KIND]


def delivery_targets() -> tuple[dict[str, Any], ...]:
    """Every place HQ knows a certificate can be installed."""

    return _declared(DELIVERY_TARGET_KIND)[DELIVERY_TARGET_KIND]


def caddy_routes() -> tuple[dict[str, Any], ...]:
    """Every Caddy route HQ declares, for the file each edge imports."""

    return _declared(CADDY_ROUTE_KIND)[CADDY_ROUTE_KIND]


class _NamesByConnection:
    """The observed name map, read at most once and only if asked.

    Resolution happens for every declaration on a page, and only certificates
    ask this. Read eagerly, a dashboard listing thirty resources would pay for
    a sweep join thirty times to answer a question none of them asked.
    """

    def __init__(self) -> None:
        self._found: dict[str, tuple[str, ...]] | None = None

    def __call__(self, connection_ref: str) -> tuple[str, ...]:
        if self._found is None:
            self._found = names_by_connection()
        return self._found.get(connection_ref, ())


def declared_machines() -> tuple[dict[str, Any], ...]:
    """Machines HQ has been told about, with the addresses that reach them.

    Most machines need no declaration: a Portainer names the ones it manages,
    a credential names what it points at. These are the rest, and they are what
    turns a forwarding address into somewhere with a name.
    """

    return _declared(MACHINE_KIND)[MACHINE_KIND]


def resolved_spec(resource: ManagedResource, targets: tuple[dict[str, Any], ...] | None = None) -> dict[str, Any]:
    """The spec as a controller would see it, falling back to the authored one.

    The only implementation: a projection that resolved a spec differently from
    the one beside it would disagree about which names a certificate covers.
    """

    # A certificate names where it installs; the settings each of those places
    # needs live on the place. Where resolution cannot happen (a target that no
    # longer exists) the authored spec stands in and the certificate covers
    # nothing, which surfaces as an uncovered name: exactly true, since HQ
    # cannot demonstrate that anything covers it.
    try:
        return resolve_provider_spec(
            resource.kind,
            resource.spec,
            context=ProviderResolutionContext(
                delivery_targets=(delivery_targets() if targets is None else targets),
                resource_key=resource.key,
                names_at=_NamesByConnection(),
                caddy_routes=caddy_routes,
            ),
        )
    except KeyError, TypeError, ValueError:
        return resource.spec


def suggest_key(kind: str, spec: dict[str, Any]) -> str:
    """A free, readable key for a declaration nobody wanted to name.

    The create form, adoption and onboarding all call this, so they suggest the
    same key even for a provider with several records per hostname.
    """

    # The provider says what to call its own records. The hostname and facet
    # are the fallback, which is what every provider that has exactly one
    # record per name would have said anyway.
    provider = PROVIDERS[kind]
    if provider.key_hint is not None:
        hint = provider.key_hint(spec)
    else:
        hostnames = provider.hostnames(spec) if provider.hostnames else ()
        hint = f"{hostnames[0]}-{provider.facet or kind}" if hostnames else kind
    # Dots become separators before slugify sees them. Left alone, slugify
    # deletes them, and "app.example.com" suggests the key "appexamplecom",
    # a permanent, unreadable name for the sake of one substitution.
    base = slugify(hint.replace(".", "-"))[:180] or slugify(kind)
    if not ManagedResource.objects.filter(key=base).exists():
        return base
    # Several records for one name is normal (a zone apex has nine) and
    # stopping to ask for a name that is merely taken is not worth the
    # interruption.
    for suffix in range(2, 100):
        candidate = f"{base[:176]}-{suffix}"
        if not ManagedResource.objects.filter(key=candidate).exists():
            return candidate
    return base


def serialize_resource(resource: ManagedResource) -> dict[str, Any]:
    provider = PROVIDERS.get(resource.kind)
    health = resource_health(resource)
    return {
        "id": str(resource.id),
        "key": resource.key,
        "kind": resource.kind,
        "enabled": resource.enabled,
        "generation": resource.generation,
        "observed_generation": resource.observed_generation,
        "in_sync": resource.generation == resource.observed_generation,
        "health": health,
        "public_effect": provider.public_effect if provider else False,
        "spec": shown_spec(resource.kind, resource.spec),
        "status": serialize_public_status(resource.status),
        "conditions": resource.conditions,
        "last_observed_at": (resource.last_observed_at.isoformat() if resource.last_observed_at else None),
        "updated_at": resource.updated_at.isoformat(),
    }


def serialize_public_status(status: dict[str, Any]) -> dict[str, Any]:
    """Return public observations without embedding downloadable artifacts."""
    public_status = {key: value for key, value in status.items() if key != "certificate_pem"}
    if status.get("certificate_pem"):
        public_status["certificate_available"] = True
    return public_status


# Each declared-resource health state as the tone every page draws it in.
RESOURCE_TONES = {
    "healthy": "good",
    "declared": "good",
    "pending": "attention",
    "drifted": "serious",
    "degraded": "serious",
    # A deploy on its way to production is not a fault (delivery_progress).
    "deploying": "neutral",
}


# What a drifted resource reads as. The finding that offers the two ways out
# keys on it, so the wording and the test for it are one value.
DRIFT_LABEL = "Changed outside HQ"


def is_drifted(resource: ManagedResource) -> bool:
    """Whether the provider holds something other than this declaration."""

    return any(
        condition.get("type") == "Drifted" and condition.get("status") is True
        for condition in resource.conditions or ()
    )


def resource_health(resource: ManagedResource) -> dict[str, str]:
    active = {condition.get("type"): condition for condition in resource.conditions if condition.get("status") is True}
    for condition_type, state, label in (
        ("Drifted", "drifted", DRIFT_LABEL),
        ("Degraded", "degraded", "Has a problem"),
        ("Ready", "healthy", "Working"),
    ):
        if condition_type in active:
            condition = active[condition_type]
            deploying = deploying_label(resource) if state == "degraded" else ""
            return {
                "state": "deploying" if deploying else state,
                "label": deploying or label,
                "reason": condition.get("reason", ""),
                "message": condition.get("message", ""),
            }
    # Nothing converges this kind, so "not reported" would never change.
    if not kind_converges(resource.kind):
        return {
            "state": "declared",
            "label": "Not checked",
            "reason": "",
            "message": "",
        }
    # HQ has asked for something the controller has not confirmed yet. That is
    # the normal state of a resource between being declared and being applied,
    # not a fault: the model already says so by carrying two generations.
    if resource.observed_generation != resource.generation:
        return {
            "state": "pending",
            "label": "Change waiting to apply",
            "reason": "",
            "message": "",
        }
    return {
        "state": "unknown",
        "label": "Not read yet",
        "reason": "",
        "message": "The controller has not read it yet.",
    }


def controller_contract(resource: ManagedResource) -> dict[str, Any]:
    """Return the minimal desired-only contract consumed by a controller."""

    def resource_status(key: str, kinds: tuple[str, ...]) -> dict[str, Any] | None:
        return ManagedResource.objects.filter(key=key, kind__in=kinds).values_list("status", flat=True).first()

    spec = resolve_provider_spec(
        resource.kind,
        resource.spec,
        context=ProviderResolutionContext(
            delivery_targets=delivery_targets(),
            resource_status=resource_status,
            resource_key=resource.key,
            names_at=_NamesByConnection(),
            caddy_routes=caddy_routes,
        ),
    )
    return {
        "schema_version": 1,
        "resource": {
            "key": resource.key,
            "kind": resource.kind,
            "generation": resource.generation,
            "enabled": resource.enabled,
            "spec": spec,
            # What the provider was last seen holding for this resource. A
            # provider finds its own record by hostname, so renaming one is
            # only possible for a controller that knows the previous name,
            # without this it searches for the new name, does not find it, and
            # creates a second record beside the one it meant to move.
            "observed": serialize_public_status(resource.status),
        },
    }


def _can_change_the_public_internet(kind: str) -> bool:
    """Whether declaring this could actually alter something publicly visible.

    The public-DNS switch guards only what the controller could act on: a
    domain declaration has no reconcile, and gating it would stop the operator
    saying what HQ owns while preventing no change to anything.
    """

    # A provider whose every action is locked cannot act, by construction.
    return any(action_kind == kind for action_kind, _ in enabled_controller_actions())


def _refuse_withheld(kind: str, current_key: str | None) -> None:
    """A withheld kind is written only by the domain that owns it, under its
    own capability: this writer would echo its spec back to the caller."""

    current = (
        ManagedResource.objects.filter(key=current_key).values_list("kind", flat=True).first() if current_key else None
    )
    if spec_withheld(kind) or (current is not None and spec_withheld(current)):
        raise PolicyError("This record is changed from its own page, not here.")


# What a write says when the declaration it changes is gone, or changed since
# it was read.
MANAGED_RECORD = Held(missing="That record was not found.", stale="That record changed after it was read.")


def _refuse_authored(command: ManagedResourceCommand, validated_spec: dict[str, Any], *, principal: Principal) -> None:
    """Refuse an authored or edited declaration HQ may not act on.

    An adopted spec is never asked: it asserts exactly what the provider
    already holds, so nothing anybody would agree to, publish or manage has
    changed.
    """

    # A kind that needs a person's agreement does not get written by a caller
    # that has not got one. The capability boundary has already held such a
    # request and answered the caller properly; this is the floor under that, so
    # a path added later cannot write one of these by not going through it.
    gap = consent_gap(command.kind, principal=principal)
    if gap:
        raise PolicyError(gap)
    if not command.enabled:
        return
    if (
        PROVIDERS[command.kind].public_effect
        and _can_change_the_public_internet(command.kind)
        and not settings.SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS
    ):
        raise PolicyError(
            "Public DNS changes are off in HQ's settings. Turn them on there "
            "to allow them, or save this record switched off."
        )
    # A declaration HQ would act on needs a connection that manages.
    if observes_only(command.kind, validated_spec):
        raise PolicyError(OBSERVES_ONLY)


@transaction.atomic
def save_managed_resource(
    command: ManagedResourceCommand,
    *,
    principal: Principal,
    current_key: str | None = None,
    expected_updated_at: str | None = None,
    copied_from_live: bool = False,
) -> dict[str, Any]:
    """Write one declaration, for a caller the capability runner or HQ itself authorized.

    ``copied_from_live`` says the spec was read from the provider moments ago
    rather than authored, which exempts it from ``_refuse_authored``: refusing
    to *write down* a record that is already published would stop nothing and
    leave every public record unadopted.
    """

    _refuse_withheld(command.kind, current_key)
    validated_spec = validate_spec(command.kind, command.spec)
    if not copied_from_live:
        _refuse_authored(command, validated_spec, principal=principal)
    provider = PROVIDERS[command.kind]

    operation = "infrastructure.resource.create" if current_key is None else "infrastructure.resource.update"
    with operation_context(interface=principal.interface, actor=principal.actor, operation=operation):
        created = current_key is None
        resource = (
            ManagedResource()
            if created
            else held(ManagedResource, MANAGED_RECORD, {"key": current_key}, expected_updated_at)
        )
        changed = (
            created
            or resource.key != command.key
            or resource.kind != command.kind
            or resource.spec != validated_spec
            or resource.enabled != command.enabled
        )
        resource.key = command.key
        resource.kind = command.kind
        resource.spec = validated_spec
        resource.enabled = command.enabled
        # Desired state is the authored spec plus whatever it resolves to, so a
        # certificate whose target moved is out of date without having been
        # edited. One function decides that, and every writer calls it.
        resource.desired_fingerprint = desired_fingerprint(
            resource.kind,
            resource.spec,
            enabled=resource.enabled,
            targets=delivery_targets(),
            resource_key=resource.key,
        )
        if not created and changed:
            resource.generation += 1
        resource.full_clean()
        resource.save()
        # A target says how a place takes a certificate. Editing one changes
        # what every certificate installed there resolves to, so their desired
        # state is recomputed here rather than waiting for each to be saved.
        if changed and provider.resolution_input:
            advance_dependents(delivery_targets())
        applying = _apply_saved(resource, principal=principal) if changed and not copied_from_live else None

    return {
        "ok": True,
        "created": created,
        "resource": serialize_resource(resource),
        "applying": applying,
    }


def _apply_saved(resource: ManagedResource, *, principal: Principal) -> dict[str, Any] | None:
    """Queue the reconcile that applies a saved declaration, as the operation queued.

    Only for a kind the controller applies on its own, which it would queue at
    its next pass anyway; queued with the write, the doorbell rings and the
    change is applied now rather than at the controller's floor. A kind whose
    reconcile is asked for by hand stays that way.
    """

    automatic = enabled_controller_actions(automatic_only=True)
    if not resource.enabled or (resource.kind, OperationRequest.Action.RECONCILE) not in automatic:
        return None
    return queue_reconcile(resource, principal=principal, reason="HQ's copy of it changed.")["operation"]
