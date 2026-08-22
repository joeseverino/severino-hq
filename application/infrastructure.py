"""Infrastructure desired state and policy-gated operation requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any

from django.conf import settings
from django.db import transaction

from control_plane.models import ManagedResource, OperationRequest
from control_plane.providers import (
    CERTIFICATE_KIND,
    DELIVERY_TARGET_KIND,
    MACHINE_KIND,
    PROVIDERS,
    resolve_provider_spec,
    controller_action_policy,
    enabled_controller_actions,
    validate_spec,
)
from control_plane.desired_state import advance_dependents, desired_fingerprint
from core.audit import operation_context

from .projection import page_size
from .cadence import ring_doorbell
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


@dataclass(frozen=True)
class OperationCommand:
    idempotency_key: str
    reason: str = ""


def list_managed_resources(*, limit: int = 50) -> dict[str, Any]:
    """List canonical public infrastructure state without provider credentials."""
    # The shared bound, not a fourth spelling of it. Written out here with the
    # ceiling as a literal, this module would have kept its own limit on the day
    # the shared one moved.
    items = [
        serialize_resource(resource)
        for resource in ManagedResource.objects.all()[: page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def _declared(*kinds: str) -> dict[str, tuple[dict[str, Any], ...]]:
    """The specs of several declaration kinds, in one read.

    Read once and passed down rather than looked up per resource: a projection
    resolving fifty declarations wants one query, not fifty -- and asking for
    two kinds separately is two queries for one page's worth of context.
    """

    found: dict[str, list[dict[str, Any]]] = {kind: [] for kind in kinds}
    for kind, spec in ManagedResource.objects.filter(
        kind__in=kinds, enabled=True
    ).values_list("kind", "spec"):
        found[kind].append(spec)
    return {kind: tuple(specs) for kind, specs in found.items()}


def context_for_resolution() -> tuple[
    tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]
]:
    """``(machines, delivery targets)`` -- everything resolution reads, once."""

    declared = _declared(MACHINE_KIND, DELIVERY_TARGET_KIND)
    return declared[MACHINE_KIND], declared[DELIVERY_TARGET_KIND]


def delivery_targets() -> tuple[dict[str, Any], ...]:
    """Every place HQ knows a certificate can be installed."""

    return _declared(DELIVERY_TARGET_KIND)[DELIVERY_TARGET_KIND]


def declared_machines() -> tuple[dict[str, Any], ...]:
    """Machines HQ has been told about, with the addresses that reach them.

    Most machines need no declaration -- a Portainer names the ones it manages,
    a credential names what it points at. These are the rest, and they are what
    turns a forwarding address into somewhere with a name.
    """

    return _declared(MACHINE_KIND)[MACHINE_KIND]


def resolved_spec(
    resource: ManagedResource, targets: tuple[dict[str, Any], ...] | None = None
) -> dict[str, Any]:
    """The spec as a controller would see it, falling back to the authored one.

    A certificate names where it installs; the settings each of those places
    needs live on the place. Where resolution cannot happen -- a target that no
    longer exists -- the authored spec stands in and the certificate covers
    nothing. That surfaces as an uncovered name, which is exactly true: HQ
    cannot demonstrate that anything covers it.

    One implementation. The service view and the domain view each had their own,
    and a projection that resolved a spec differently from the one beside it
    would disagree about which names a certificate covers -- while both claimed
    to be reading the same declaration.
    """

    from control_plane.providers import ProviderResolutionContext

    try:
        return resolve_provider_spec(
            resource.kind,
            resource.spec,
            context=ProviderResolutionContext(
                delivery_targets=(
                    delivery_targets() if targets is None else targets
                ),
                resource_key=resource.key,
            ),
        )
    except (KeyError, TypeError, ValueError):
        return resource.spec


def suggest_key(kind: str, spec: dict[str, Any]) -> str:
    """A free, readable key for a declaration nobody wanted to name.

    One implementation, because there were three: the create form derived a key
    one way, adoption another, and the onboarding flow a third. They agreed
    while every provider had one record per hostname and diverged the moment one
    did not -- the form suggesting a key built from a hostname that a TXT record
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
    # deletes them, and "app.example.com" suggests the key "appexamplecom" --
    # a permanent, unreadable name for the sake of one substitution.
    base = slugify(hint.replace(".", "-"))[:180] or slugify(kind)
    if not ManagedResource.objects.filter(key=base).exists():
        return base
    # Several records for one name is normal -- a zone apex has nine -- and
    # stopping to ask for a name that is merely taken is not worth the
    # interruption.
    for suffix in range(2, 100):
        candidate = f"{base[:176]}-{suffix}"
        if not ManagedResource.objects.filter(key=candidate).exists():
            return candidate
    return base


def get_managed_resource(key: str) -> dict[str, Any]:
    """Return resource state and structured operation history as one contract."""
    try:
        resource = ManagedResource.objects.get(key=key)
    except ManagedResource.DoesNotExist as exc:
        raise NotFoundError(f"Managed resource {key!r} was not found.") from exc
    return {
        "resource": serialize_resource(resource),
        "operations": [
            operation_summary(operation) for operation in resource.operations.all()[:20]
        ],
    }


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
            resource.last_observed_at.isoformat()
            if resource.last_observed_at
            else None
        ),
        "updated_at": resource.updated_at.isoformat(),
    }


def serialize_public_status(status: dict[str, Any]) -> dict[str, Any]:
    """Return public observations without embedding downloadable artifacts."""
    public_status = {key: value for key, value in status.items() if key != "certificate_pem"}
    if status.get("certificate_pem"):
        public_status["certificate_available"] = True
    return public_status


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
    # A declaration that records only a responsibility has nothing to converge,
    # so "not reported" describes something that is never going to happen.
    provider = PROVIDERS.get(resource.kind)
    if provider is not None and provider.declaration_only:
        return {
            "state": "declared",
            "label": "Recorded",
            "reason": "",
            "message": "",
        }
    # HQ has asked for something the controller has not confirmed yet. That is
    # the normal state of a resource between being declared and being applied,
    # not a fault -- the model already says so by carrying two generations.
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


def serialize_operation(operation: OperationRequest) -> dict[str, Any]:
    return {
        "id": str(operation.id),
        "resource": operation.resource.key,
        "action": operation.action,
        "state": operation.state,
        "reason": operation.reason,
        "requested_actor": operation.requested_actor,
        "requested_interface": operation.requested_interface,
        "created_at": operation.created_at.isoformat(),
        "completed_at": (
            operation.completed_at.isoformat() if operation.completed_at else None
        ),
        "claimed_by": operation.claimed_by,
        "lease_expires_at": (
            operation.lease_expires_at.isoformat()
            if operation.lease_expires_at
            else None
        ),
        "attempt_count": operation.attempt_count,
        "result": operation.result,
    }


def operation_summary(operation: OperationRequest) -> dict[str, Any]:
    """Project one operation into concise operator guidance and structured evidence."""
    result = operation.result or {}
    status = result.get("status") or {}
    conditions = result.get("conditions") or []
    evidence = status.get("consumers") or []
    affected = [item for item in evidence if item.get("matches_expected") is False]
    condition = next(
        (item for item in conditions if item.get("status") is True), None
    )
    message = result.get("message") or operation.reason

    if operation.state == OperationRequest.State.QUEUED:
        headline = "Waiting for the controller"
        guidance = "HQ will claim this automatically; no manual server action is needed."
    elif operation.state == OperationRequest.State.CLAIMED:
        headline = "Controller is applying and verifying the change"
        guidance = "The operation is leased; wait for verification before retrying."
    elif operation.state == OperationRequest.State.FAILED:
        headline = (condition or {}).get("message") or message or "Provider operation failed"
        guidance = (
            "Review the affected targets and provider reason, correct the canonical "
            "desired state or provider access, then reconcile again."
        )
    else:
        headline = message or "Operation completed successfully"
        guidance = "No action is required."

    return {
        "id": str(operation.id),
        "action": operation.action,
        "action_label": operation.get_action_display(),
        "state": operation.state,
        "state_label": operation.get_state_display(),
        "headline": headline,
        "guidance": guidance,
        "automatic": operation.requested_interface == "controller",
        "requested_actor": operation.requested_actor,
        "requested_interface": operation.requested_interface,
        "created_at": operation.created_at.isoformat(),
        "completed_at": (
            operation.completed_at.isoformat() if operation.completed_at else None
        ),
        "attempt_count": operation.attempt_count,
        "reason": operation.reason,
        "condition": condition,
        "affected": affected,
        "evidence": evidence,
        "expected_fingerprint_sha256": status.get("expected_fingerprint_sha256", ""),
        "raw_result": result,
    }


def controller_contract(resource: ManagedResource) -> dict[str, Any]:
    """Return the minimal desired-only contract consumed by a controller."""
    from control_plane.providers import ProviderResolutionContext, resolve_provider_spec

    def resource_status(key: str, kind: str) -> dict[str, Any] | None:
        certificate = ManagedResource.objects.filter(
            key=key, kind=kind
        ).values_list("status", flat=True).first()
        return certificate

    spec = resolve_provider_spec(
        resource.kind,
        resource.spec,
        context=ProviderResolutionContext(
            delivery_targets=delivery_targets(),
            resource_status=resource_status,
            resource_key=resource.key,
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
            # only possible for a controller that knows the previous name --
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
    could run -- gating it prevented the operator from saying what HQ owns while
    preventing no change to anything.

    So the question is not "is this public" but "could the controller act on
    it". A provider whose every action is locked cannot, by construction.
    """

    return any(
        action_kind == kind for action_kind, _ in enabled_controller_actions()
    )


@transaction.atomic
def save_managed_resource(
    command: ManagedResourceCommand,
    *,
    principal: Principal,
    current_key: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    validated_spec = validate_spec(command.kind, command.spec)
    provider = PROVIDERS[command.kind]
    if (
        command.enabled
        and provider.public_effect
        and _can_change_the_public_internet(command.kind)
        and not getattr(settings, "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False)
    ):
        raise PolicyError(
            "Changing public DNS is switched off in this deployment. Set "
            "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS to allow it, or save "
            "this resource disabled to record the declaration without acting "
            "on it."
        )

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


def _resource_for_operation(key: str) -> ManagedResource:
    try:
        return ManagedResource.objects.select_for_update().get(key=key)
    except ManagedResource.DoesNotExist as exc:
        raise NotFoundError(f"Managed resource {key!r} was not found.") from exc


def _queue_operation(
    resource: ManagedResource,
    command: OperationCommand,
    *,
    principal: Principal,
    action: str,
    require_enabled: bool = True,
) -> dict[str, Any]:
    if require_enabled and not resource.enabled:
        raise PolicyError(f"Managed resource {resource.key!r} is disabled.")
    allowed, explanation = controller_action_policy(resource.kind, action)
    if not allowed:
        raise PolicyError(explanation)
    existing = OperationRequest.objects.filter(
        idempotency_key=command.idempotency_key
    ).first()
    if existing:
        if existing.resource_id != resource.id or existing.action != action:
            raise PolicyError("Idempotency key is already used by another operation.")
        return {"ok": True, "queued": False, "operation": serialize_operation(existing)}
    active = OperationRequest.objects.filter(
        resource=resource,
        action=action,
        state__in=(
            OperationRequest.State.QUEUED,
            OperationRequest.State.CLAIMED,
        ),
    ).first()
    if active:
        return {"ok": True, "queued": False, "operation": serialize_operation(active)}

    operation = OperationRequest.objects.create(
        resource=resource,
        action=action,
        requested_actor=principal.actor,
        requested_interface=principal.interface,
        reason=command.reason,
        idempotency_key=command.idempotency_key,
        input={"generation": resource.generation},
    )
    # Every operation of every kind is created here, so this is the one place
    # that has to ring. The controller still pulls the work; this only says
    # there is some, which is what removes the wait between pressing Save and
    # anything happening.
    transaction.on_commit(ring_doorbell)
    return {"ok": True, "queued": True, "operation": serialize_operation(operation)}


@transaction.atomic
def request_reconcile(
    command: OperationCommand,
    *,
    principal: Principal,
    current_key: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = _resource_for_operation(current_key)
    provider = PROVIDERS[resource.kind]
    if provider.public_effect and not getattr(
        settings, "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False
    ):
        raise PolicyError("Public DNS reconciliation is disabled.")
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="infrastructure.reconcile.request",
    ):
        return _queue_operation(
            resource,
            command,
            principal=principal,
            action=OperationRequest.Action.RECONCILE,
        )


@transaction.atomic
def _contained_keys(resource: ManagedResource) -> list[str]:
    """The declarations this one holds, as the provider describes the tie.

    Named by the provider rather than matched here, so the second kind with
    anything inside it is a registry entry and not another branch in this
    function.
    """

    relation = PROVIDERS[resource.kind].contains
    if relation is None:
        return []
    kind, their_field, my_field = relation
    value = str(resource.spec.get(my_field, "")).strip().lower()
    if not value:
        return []
    return list(
        ManagedResource.objects.filter(
            kind=kind, **{f"spec__{their_field}__iexact": value}
        ).values_list("key", flat=True)
    )


@transaction.atomic
def _forget_declaration(
    resource: ManagedResource, command: OperationCommand, *, principal: Principal
) -> dict[str, Any]:
    """Stop being responsible for something HQ never created.

    Nothing is queued and nothing is deleted at the provider, because there is
    nothing there that HQ made. A domain exists whether or not HQ has heard of
    it; the declaration only ever recorded that HQ was made responsible for it,
    so removing the declaration is the whole of the operation.

    The declarations *inside* it go too. Left behind, HQ would keep reconciling
    records in a domain it is no longer responsible for -- still writing to a
    zone the operator had just said was not its business, which is the one
    outcome this has to avoid. They are forgotten rather than deleted: the
    records stay exactly as they are at the provider, which is what stepping
    back means.
    """

    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="infrastructure.resource.forget",
    ):
        contained = _contained_keys(resource)
        ManagedResource.objects.filter(key__in=contained).delete()
        key = resource.key
        resource.delete()
        return {
            "ok": True,
            "queued": False,
            "forgotten": key,
            "released": contained,
            "reason": command.reason,
        }


@transaction.atomic
def request_lifecycle(
    command: OperationCommand,
    *,
    principal: Principal,
    current_key: str,
    action: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Ask the controller to start, stop or restart what a declaration names.

    Not reconciliation. Cycling a container does not move the world toward a
    declaration -- it is a thing asked for once, about something already exactly
    as declared -- so it neither bumps the generation nor waits on one.

    Which verbs exist is the capability registry's to say, so a controller that
    does not implement one refuses here rather than queueing work nothing will
    claim.
    """

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = _resource_for_operation(current_key)
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation=f"infrastructure.{action}.request",
    ):
        return _queue_operation(
            resource, command, principal=principal, action=action
        )


def request_removal(
    command: OperationCommand,
    *,
    principal: Principal,
    current_key: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Queue removal of the thing this declaration describes.

    Deliberately not a plain row delete. The record lives at a provider, not in
    HQ, so forgetting the declaration would abandon the rewrite or proxy host
    rather than remove it -- and nothing would be left pointing at the orphan.
    HQ drops its own row only once a controller reports the provider is clear.

    Removal is queued even for a disabled resource: disabling stops HQ
    reconciling a declaration, which is exactly the state something is left in
    just before an operator decides to be rid of it.
    """

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = _resource_for_operation(current_key)
    if PROVIDERS[resource.kind].declaration_only:
        return _forget_declaration(resource, command, principal=principal)
    allowed, explanation = controller_action_policy(
        resource.kind, OperationRequest.Action.DELETE
    )
    if not allowed:
        raise PolicyError(explanation)
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="infrastructure.resource.remove",
    ):
        # Bypasses the enabled check in _queue_operation on purpose: that guard
        # exists to stop HQ converging a paused declaration, and removal is the
        # opposite of converging it.
        return _queue_operation(
            resource,
            command,
            principal=principal,
            action=OperationRequest.Action.DELETE,
            require_enabled=False,
        )


def certificate_renewal_allowed(resource: ManagedResource) -> tuple[bool, str]:
    if resource.kind != CERTIFICATE_KIND:
        return False, "Only tls.certificate resources may be renewed."
    if not resource.enabled:
        return False, "The certificate resource is disabled."
    allowed, explanation = controller_action_policy(
        resource.kind, OperationRequest.Action.RENEW
    )
    if not allowed:
        return False, explanation
    if any(
        condition.get("status") is True
        and condition.get("type") in {"Drifted", "Degraded"}
        for condition in resource.conditions
    ):
        return True, "A consumer is drifted or degraded."

    not_after = resource.status.get("not_after")
    if not not_after:
        return True, "No verified certificate expiry has been reported."
    try:
        expiry = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True, "The reported certificate expiry is invalid."
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    days_left = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
    whole_days_left = max(0, math.ceil(days_left))
    renewal_window = resource.spec.get("renewal_window_days", 30)
    if days_left <= renewal_window:
        return True, f"Certificate has {whole_days_left} days remaining."
    return (
        False,
        f"Renewal opens at {renewal_window} days; "
        f"certificate has {whole_days_left} days remaining.",
    )


@transaction.atomic
def request_certificate_renewal(
    command: OperationCommand,
    *,
    principal: Principal,
    current_key: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    del expected_updated_at
    principal.require(Capability.REQUEST_CERTIFICATE_RENEWAL)
    resource = _resource_for_operation(current_key)
    allowed, explanation = certificate_renewal_allowed(resource)
    if not allowed:
        raise PolicyError(explanation)
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="certificate.renew.request",
    ):
        result = _queue_operation(
            resource,
            command,
            principal=principal,
            action=OperationRequest.Action.RENEW,
        )
    result["policy"] = {"allowed": True, "explanation": explanation}
    return result
