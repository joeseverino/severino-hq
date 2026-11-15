"""Infrastructure desired state and policy-gated operation requests."""

from dataclasses import dataclass
from typing import Annotated, Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import Field
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
lazy from hq.domains.control_plane.provider_spec import ProviderResolutionContext

from .adoption import Live, require_writable
from .command_fields import Choices, Describe
from .conditions import DEGRADED, DRIFTED, READY, holding
from .integration_specs import Kept, keeps
from .labels import lower_first
from .projection import keyset_page, read_once
from .record_errors import PolicyError
from .record_saves import Held, as_sent, found, held, written
from .security import Principal
from .tone import Tone, Toned
lazy from .asks import APPLIES_SHORTLY
lazy from .cadence import ask, swept
lazy from .capabilities import Consented
lazy from .delivery_progress import deploying_label
lazy from .locate import names_by_connection
lazy from .resource_capabilities import kind_converges
lazy from .resource_operations import OperationItem, ResourceSaved, queue_reconcile
lazy from .resource_records import HealthPart, ManagedResourceItem

# Each field a managed record is created and updated by, said once for both.
type ManagedResourceKey = Annotated[str, Describe("The key the record is stored under.")]
type ManagedResourceKind = Annotated[
    str,
    # The registry's kinds, the only ones validate_spec accepts.
    Choices((kind, registry_label(kind)) for kind in PROVIDERS),
    Describe("What type of record it is."),
]
type ManagedResourceSpec = Annotated[dict[str, Any], Describe("What HQ expects of it, in the shape its type takes.")]


@dataclass(frozen=True, slots=True)
class ManagedResourceCommand:
    key: ManagedResourceKey
    kind: Annotated[ManagedResourceKind, Field(title="Type")]
    spec: Annotated[ManagedResourceSpec, Field(title="Config")]
    enabled: Annotated[bool, Describe("Whether HQ keeps the live record as it expects.")] = True


def list_managed_resources(*, limit: int = 50, kind: str | None = None, kinds: str | None = None) -> dict[str, Any]:
    """List canonical public infrastructure state without provider credentials, by kind then key."""
    resources = ManagedResource.objects.all()
    if kind and kinds:
        raise ValueError("Choose either kind or kinds, not both.")
    if kind:
        resources = resources.filter(kind=kind)
    elif kinds:
        resources = resources.filter(kind__in=kinds.split(","))
    page = keyset_page(resources, ("kind", "key"), limit)
    return {**page, "items": [serialize_resource(resource) for resource in page["items"]]}


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


def serialize_resource(resource: ManagedResource) -> ManagedResourceItem:
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


class ResourceState(Toned):
    """A declared record's health in one word, as the API answers it, beside
    the tone every page draws it in. Its words on a page are its record's
    (``resource_health``'s label)."""

    HEALTHY = "healthy", Tone.GOOD
    # Nothing converges its kind, so it is as declared.
    DECLARED = "declared", Tone.GOOD
    PENDING = "pending", Tone.ATTENTION
    DRIFTED = "drifted", Tone.SERIOUS
    DEGRADED = "degraded", Tone.SERIOUS
    # A deploy on its way to production is not a fault (delivery_progress).
    DEPLOYING = "deploying", Tone.NEUTRAL
    UNKNOWN = "unknown", Tone.NEUTRAL


class RecordState(Toned):
    """Where one record stands on its page and its row
    (``resource_context.record_status``), in the word ``data-record-status``
    says, beside the tone its label and detail are drawn in."""

    OFF = "off", Tone.NEUTRAL
    DRIFTED = "drifted", Tone.SERIOUS
    DEPLOYING = "deploying", Tone.ATTENTION
    DEGRADED = "degraded", Tone.SERIOUS
    PENDING = "pending", Tone.ATTENTION
    UNSEEN = "unseen", Tone.NEUTRAL
    WORKING = "working", Tone.GOOD
    RECORDED = "recorded", Tone.NEUTRAL
    UNREAD = "unread", Tone.NEUTRAL


# What a drifted resource reads as. The finding that offers the two ways out
# keys on it, so the wording and the test for it are one value.
DRIFT_LABEL = "Changed outside HQ"
# What a record whose kind is read reads as while it reports no state of its
# own. The finding that says HQ could not check it keys on it.
NOT_READ_LABEL = "Not read yet"


def resource_health(resource: ManagedResource) -> HealthPart:
    active = {condition.get("type"): condition for condition in holding(resource.conditions)}
    for condition_type, state, label in (
        (DRIFTED, ResourceState.DRIFTED, DRIFT_LABEL),
        (DEGRADED, ResourceState.DEGRADED, "Has a problem"),
        (READY, ResourceState.HEALTHY, "Working"),
    ):
        if condition_type in active:
            condition = active[condition_type]
            deploying = deploying_label(resource) if state is ResourceState.DEGRADED else ""
            return {
                "state": ResourceState.DEPLOYING if deploying else state,
                "label": deploying or label,
                "reason": condition.get("reason", ""),
                "message": condition.get("message", ""),
            }
    # Nothing converges this kind, so "not reported" would never change.
    if not kind_converges(resource.kind):
        return {
            "state": ResourceState.DECLARED,
            "label": "Not checked",
            "reason": "",
            "message": "",
        }
    # HQ has asked for something the controller has not confirmed yet. That is
    # the normal state of a resource between being declared and being applied,
    # not a fault: the model already says so by carrying two generations.
    if resource.observed_generation != resource.generation:
        return {
            "state": ResourceState.PENDING,
            "label": "Change waiting to apply",
            "reason": "",
            "message": "",
        }
    return {
        "state": ResourceState.UNKNOWN,
        "label": NOT_READ_LABEL,
        "reason": "",
        "message": "HQ has not read it yet.",
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


def _refuse_withheld(kind: str, held_kind: str) -> None:
    """A withheld kind is written only by the domain that owns it, under its
    own capability: this writer would echo its spec back to the caller. Asked
    of the kind to be written and of the record's kind as it is locked."""

    if spec_withheld(kind) or spec_withheld(held_kind):
        raise PolicyError("This record is changed from its own page, not here.")


# What a write says when the declaration it changes is gone, or changed since
# it was read.
MANAGED_RECORD = Held(missing="That record was not found.", stale="That record changed after it was read.")


def _refuse_authored(
    command: ManagedResourceCommand, held_kind: str, validated_spec: dict[str, Any], *, consented: Consented
) -> None:
    """Refuse an authored or edited declaration HQ may not act on.

    An adopted spec is never asked: it asserts exactly what the provider
    already holds, so nothing anybody would agree to, publish or manage has
    changed.
    """

    # Asked of the kind to be written and of the record's kind as it is
    # locked, so a gated record is not rewritten as another kind either.
    consented.require(command.kind)
    consented.require(held_kind)
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
    require_writable(command.kind, validated_spec)


def _stored(key: str) -> dict[str, Any]:
    return as_sent(found(ManagedResource, MANAGED_RECORD, {"key": key}), ManagedResourceCommand, {})


@keeps(Kept(_stored))
@transaction.atomic
def save_managed_resource(
    command: ManagedResourceCommand,
    *,
    consented: Consented,
    current_key: str | None = None,
    expected_updated_at: str | None = None,
    sent: frozenset[str] | None = None,
) -> ResourceSaved:
    """Write one declaration a run the capability runner consented to authored, and apply it.

    An update writes the fields it was ``sent`` over the record as locked, and
    the spec is validated and judged as it will be written. A field not sent
    is the locked row's, never the read made before the lock; a caller that
    must not write over a change since its read sends ``expected_updated_at``,
    which refuses the write instead.
    """

    return _save(
        command,
        principal=consented.principal,
        consented=consented,
        current_key=current_key,
        expected_updated_at=expected_updated_at,
        sent=sent,
    )


@transaction.atomic
def save_adopted(
    live: Live, *, key: str, principal: Principal, enabled: bool = True, current_key: str | None = None
) -> ResourceSaved:
    """Write down, under ``key``, a declaration of what HQ's last read found live.

    Asks no consent and applies nothing: HQ's copy then equals what is live,
    so nothing anybody would agree to, publish or manage has changed, and
    refusing to *write down* a record that is already published would stop
    nothing and leave every public record unadopted. It takes a ``Live``,
    which only a read of the last sweep makes, so nothing authored reaches
    it. HQ's own sweep adopts through this, outside any capability run, and
    so does keeping what is live over HQ's copy (``accept_observed``).
    """

    command = ManagedResourceCommand(key=key, kind=live.kind, spec=live.spec, enabled=enabled)
    return _save(command, principal=principal, consented=None, current_key=current_key)


def _save(
    command: ManagedResourceCommand,
    *,
    principal: Principal,
    consented: Consented | None,
    current_key: str | None = None,
    expected_updated_at: str | None = None,
    sent: frozenset[str] | None = None,
) -> ResourceSaved:
    """One declaration written, authored under ``consented`` or adopted with none."""

    created = current_key is None
    resource = (
        ManagedResource()
        if created
        else held(ManagedResource, MANAGED_RECORD, {"key": current_key}, expected_updated_at)
    )
    # HQ's copy as the change found it, which an apply by hand may only replace.
    replaced = {} if created else dict(resource.spec)
    if not created:
        command = ManagedResourceCommand(**{**as_sent(resource, ManagedResourceCommand, {}), **written(command, sent)})
    _refuse_withheld(command.kind, resource.kind)
    validated_spec = validate_spec(command.kind, command.spec)
    if consented is not None:
        _refuse_authored(command, resource.kind, validated_spec, consented=consented)
    provider = PROVIDERS[command.kind]
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
    if not created and not resource.enabled:
        _call_off_waiting(resource)
    # A target says how a place takes a certificate. Editing one changes
    # what every certificate installed there resolves to, so their desired
    # state is recomputed here rather than waiting for each to be saved.
    if changed and provider.resolution_input:
        advance_dependents(delivery_targets())
    applying = _apply_saved(resource, replaced, consented=consented) if changed and consented is not None else None

    return {
        "ok": True,
        "created": created,
        "resource": serialize_resource(resource),
        "applying": applying,
        "message": f"{'Added' if created else 'Updated'} “{resource.key}”."
        + (f" {APPLIES_SHORTLY}" if applying else ""),
    }


def _call_off_waiting(resource: ManagedResource) -> None:
    """Cancel the work queued for a record switched off, all but its removal.

    Switching off pauses HQ's changes to the record. Every queue path checks
    the switch when it queues; this holds the work already waiting, row by
    row so each cancel is audited as the switch-off's. A removal is the one
    thing still asked of a switched-off record (``request_removal``).
    """

    now = timezone.now()
    said = f"The {lower_first(registry_label(resource.kind))} was switched off in HQ before this ran."
    waiting = resource.operations.select_for_update().filter(state=OperationRequest.State.QUEUED)
    for operation in waiting.exclude(action=OperationRequest.Action.DELETE):
        operation.state = OperationRequest.State.CANCELLED
        operation.completed_at = now
        operation.result = {
            "ok": False,
            "message": said,
            "reason": "SwitchedOff",
        }
        operation.save(update_fields=("state", "completed_at", "result", "updated_at"))


SAVED_REASON = "HQ's copy of it changed."


def _apply_saved(resource: ManagedResource, replaced: dict[str, Any], *, consented: Consented) -> OperationItem | None:
    """Queue the reconcile that applies a saved declaration, as the save's own act.

    Every kind's change is applied by the save that made it, so one consent
    (a person's save, an approval) covers the apply and nothing is asked twice.
    A kind the controller converges on its own would be queued at its next
    pass anyway. A kind applied only by hand writes HQ's whole copy over what
    is live, so its reconcile carries the copy it ``replaced``, and the claim
    applies it only over a live record read since then as that copy
    (``controller.over_live``): the live change is then exactly the change
    that was saved or approved, and a live record that drifted is refused.
    """

    kind_action = (resource.kind, OperationRequest.Action.RECONCILE)
    if not resource.enabled or kind_action not in enabled_controller_actions():
        return None
    if kind_action in enabled_controller_actions(automatic_only=True):
        return queue_reconcile(resource, consented=consented, reason=SAVED_REASON)["operation"]
    if not swept(resource.kind):
        # Nothing reads it live, so nothing could show the apply stays within the change.
        return None
    ask(reads=(resource.kind,))
    return queue_reconcile(resource, consented=consented, reason=SAVED_REASON, replaces=replaced)["operation"]
