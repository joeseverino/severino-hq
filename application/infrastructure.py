"""Infrastructure desired state and policy-gated operation requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction

from control_plane.models import ManagedResource
from control_plane.providers import (
    PROVIDERS,
    resolve_provider_spec,
    enabled_controller_actions,
    validate_spec,
)
from control_plane.provider_adapters.caddy import CADDY_ROUTE_KIND
from control_plane.provider_adapters.declarations import (
    DELIVERY_TARGET_KIND,
    MACHINE_KIND,
)
from control_plane.desired_state import advance_dependents, desired_fingerprint
from core.audit import operation_context

from .adoption import OBSERVES_ONLY, observes_only
from .approvals import consent_gap
from .projection import page_size, read_once
from .security import Capability, Principal


class NotFoundError(ValueError):
    """A requested managed resource does not exist."""


class PolicyError(ValueError):
    """An operation is valid in shape but disallowed by current policy."""


@dataclass(frozen=True)
class ManagedResourceCommand:
    key: str
    kind: str
    spec: dict[str, Any]
    enabled: bool = True


def list_managed_resources(
    *, limit: int = 50, kind: str | None = None, kinds: str | None = None
) -> dict[str, Any]:
    """List canonical public infrastructure state without provider credentials."""
    # The shared bound, not a fourth spelling of it. Written out here with the
    # ceiling as a literal, this module would have kept its own limit on the day
    # the shared one moved.
    resources = ManagedResource.objects.all()
    if kind and kinds:
        raise ValueError("Choose either kind or kinds, not both.")
    if kind:
        resources = resources.filter(kind=kind)
    elif kinds:
        resources = resources.filter(kind__in=kinds.split(","))
    items = [serialize_resource(resource) for resource in resources[: page_size(limit)]]
    return {"items": items, "count": len(items)}


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


def context_for_resolution() -> tuple[
    tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]
]:
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
            # Deferred on purpose: locate imports this module, so at module
            # scope this is a cycle. Resolved on first call, which is also the
            # first point the answer is wanted.
            from .locate import names_by_connection

            self._found = names_by_connection()
        return self._found.get(connection_ref, ())


def declared_machines() -> tuple[dict[str, Any], ...]:
    """Machines HQ has been told about, with the addresses that reach them.

    Most machines need no declaration: a Portainer names the ones it manages,
    a credential names what it points at. These are the rest, and they are what
    turns a forwarding address into somewhere with a name.
    """

    return _declared(MACHINE_KIND)[MACHINE_KIND]


def resolved_spec(
    resource: ManagedResource, targets: tuple[dict[str, Any], ...] | None = None
) -> dict[str, Any]:
    """The spec as a controller would see it, falling back to the authored one.

    A certificate names where it installs; the settings each of those places
    needs live on the place. Where resolution cannot happen (a target that no
    longer exists) the authored spec stands in and the certificate covers
    nothing. That surfaces as an uncovered name, which is exactly true: HQ
    cannot demonstrate that anything covers it.

    One implementation. The service view and the domain view each had their own,
    and a projection that resolved a spec differently from the one beside it
    would disagree about which names a certificate covers, while both claimed
    to be reading the same declaration.
    """

    from control_plane.provider_spec import ProviderResolutionContext

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
    except (KeyError, TypeError, ValueError):
        return resource.spec


def suggest_key(kind: str, spec: dict[str, Any]) -> str:
    """A free, readable key for a declaration nobody wanted to name.

    One implementation, because there were three: the create form derived a key
    one way, adoption another, and the onboarding flow a third. They agreed
    while every provider had one record per hostname and diverged the moment one
    did not: the form suggesting a key built from a hostname that a TXT record
    does not have.

    The provider says what to call its own records. The hostname and facet are
    the fallback, which is what every provider that has exactly one record per
    name would have said anyway.
    """

    from django.utils.text import slugify

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
        "spec": resource.spec,
        "status": serialize_public_status(resource.status),
        "conditions": resource.conditions,
        "last_observed_at": (
            resource.last_observed_at.isoformat() if resource.last_observed_at else None
        ),
        "updated_at": resource.updated_at.isoformat(),
    }


def serialize_public_status(status: dict[str, Any]) -> dict[str, Any]:
    """Return public observations without embedding downloadable artifacts."""
    public_status = {
        key: value for key, value in status.items() if key != "certificate_pem"
    }
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
}


def resource_health(resource: ManagedResource) -> dict[str, str]:
    active = {
        condition.get("type"): condition
        for condition in resource.conditions
        if condition.get("status") is True
    }
    for condition_type, state, label in (
        ("Drifted", "drifted", "Drift detected"),
        ("Degraded", "degraded", "Needs attention"),
        ("Ready", "healthy", "Healthy"),
    ):
        if condition_type in active:
            condition = active[condition_type]
            return {
                "state": state,
                "label": label,
                "reason": condition.get("reason", ""),
                "message": condition.get("message", ""),
            }
    # Nothing converges this kind, so "not reported" would never change.
    from .resource_capabilities import kind_converges

    if not kind_converges(resource.kind):
        return {
            "state": "declared",
            "label": "Recorded",
            "reason": "",
            "message": "",
        }
    # HQ has asked for something the controller has not confirmed yet. That is
    # the normal state of a resource between being declared and being applied,
    # not a fault: the model already says so by carrying two generations.
    if resource.observed_generation != resource.generation:
        return {
            "state": "pending",
            "label": "Awaiting first check",
            "reason": "",
            "message": "",
        }
    return {
        "state": "unknown",
        "label": "Not observed",
        "reason": "",
        "message": "The controller has not reported health.",
    }


def controller_contract(resource: ManagedResource) -> dict[str, Any]:
    """Return the minimal desired-only contract consumed by a controller."""
    from control_plane.providers import resolve_provider_spec
    from control_plane.provider_spec import ProviderResolutionContext

    def resource_status(key: str, kinds: tuple[str, ...]) -> dict[str, Any] | None:
        return (
            ManagedResource.objects.filter(key=key, kind__in=kinds)
            .values_list("status", flat=True)
            .first()
        )

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

    The switch this guards exists because public DNS is the one surface where a
    mistake is immediately everybody's problem. It is not a reason to refuse
    every resource that happens to be publicly visible: a domain declaration
    records which zones HQ is responsible for and has no reconcile a controller
    could run: gating it prevented the operator from saying what HQ owns while
    preventing no change to anything.

    So the question is not "is this public" but "could the controller act on
    it". A provider whose every action is locked cannot, by construction.
    """

    return any(action_kind == kind for action_kind, _ in enabled_controller_actions())


@transaction.atomic
def save_managed_resource(
    command: ManagedResourceCommand,
    *,
    principal: Principal,
    current_key: str | None = None,
    expected_updated_at: str | None = None,
    copied_from_live: bool = False,
) -> dict[str, Any]:
    """Write one declaration.

    ``copied_from_live`` says the spec was read from the provider moments ago
    rather than authored. It exempts the write from the public-DNS switch
    below, and only that: an adopted record asserts exactly what the provider
    already holds, so reconciling it changes nothing. The switch exists to stop
    HQ changing public DNS, and refusing to *write down* a record that is
    already published stopped nothing: it left every public record listed as
    unadopted, on a deployment that had deliberately said "do not change these"
    and was then told it could not describe them either.
    """

    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    validated_spec = validate_spec(command.kind, command.spec)
    # A kind that needs a person's agreement does not get written by a caller
    # that has not got one. The capability boundary has already held such a
    # request and answered the caller properly; this is the floor under that, so
    # a path added later cannot write one of these by not going through it.
    # Exempted for an adopted spec for the same reason the public-DNS switch
    # below is: it asserts exactly what the provider already holds, so nothing
    # anybody would have to agree to has changed.
    gap = "" if copied_from_live else consent_gap(command.kind, principal=principal)
    if gap:
        raise PolicyError(gap)
    provider = PROVIDERS[command.kind]
    if (
        command.enabled
        and not copied_from_live
        and provider.public_effect
        and _can_change_the_public_internet(command.kind)
        and not getattr(settings, "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False)
    ):
        raise PolicyError(
            "Public DNS changes are off. Set "
            "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS to allow them, or save "
            "this resource disabled."
        )
    # An authored or edited declaration is one HQ would act on; its connection
    # must manage. An adopted spec restates what the provider holds.
    if command.enabled and not copied_from_live and observes_only(command.kind, validated_spec):
        raise PolicyError(OBSERVES_ONLY)

    operation = (
        "infrastructure.resource.create"
        if current_key is None
        else "infrastructure.resource.update"
    )
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        if current_key is None:
            resource = ManagedResource()
            created = True
        else:
            try:
                resource = ManagedResource.objects.select_for_update().get(
                    key=current_key
                )
            except ManagedResource.DoesNotExist as exc:
                raise NotFoundError(
                    f"Managed resource {current_key!r} was not found."
                ) from exc
            if (
                expected_updated_at
                and resource.updated_at.isoformat() != expected_updated_at
            ):
                raise PolicyError(
                    f"Managed resource {current_key!r} changed after it was read."
                )
            created = False

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
            resource.enabled,
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
        provider = PROVIDERS.get(resource.kind)
        if changed and provider is not None and provider.resolution_input:
            advance_dependents(delivery_targets())

    return {
        "ok": True,
        "created": created,
        "resource": serialize_resource(resource),
    }
