"""Infrastructure desired state and policy-gated operation requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.conf import settings
from django.db import transaction

from control_plane.models import ManagedResource, OperationRequest
from control_plane.providers import (
    PROVIDERS,
    controller_action_policy,
    validate_spec,
)
from core.audit import operation_context

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


def serialize_resource(resource: ManagedResource) -> dict[str, Any]:
    provider = PROVIDERS.get(resource.kind)
    health = resource_health(resource)
    return {
        "id": str(resource.id),
        "key": resource.key,
        "kind": resource.kind,
        "declaration_source": resource.declaration_source,
        "enabled": resource.enabled,
        "generation": resource.generation,
        "observed_generation": resource.observed_generation,
        "in_sync": resource.generation == resource.observed_generation,
        "health": health,
        "public_effect": provider.public_effect if provider else False,
        "spec": resource.spec,
        "status": resource.status,
        "conditions": resource.conditions,
        "last_observed_at": (
            resource.last_observed_at.isoformat()
            if resource.last_observed_at
            else None
        ),
        "updated_at": resource.updated_at.isoformat(),
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


def controller_contract(resource: ManagedResource) -> dict[str, Any]:
    """Return the minimal desired-only contract consumed by a controller."""
    spec = validate_spec(resource.kind, resource.spec)
    if resource.kind == "tls.certificate":
        from control_plane.providers import validate_resolved_certificate
        from control_plane.topology import resolve_certificate

        spec = validate_resolved_certificate(
            {
                **resolve_certificate(resource.spec["topology_ref"]),
                "renewal_window_days": resource.spec["renewal_window_days"],
            }
        )
    elif resource.kind == "npm.proxy_host" and spec["certificate_resource"]:
        certificate = ManagedResource.objects.filter(
            key=spec["certificate_resource"], kind="tls.certificate"
        ).first()
        certificate_id = (
            certificate.status.get("npm_certificate_id") if certificate else None
        )
        spec["certificate_id"] = certificate_id
    return {
        "schema_version": 1,
        "resource": {
            "key": resource.key,
            "kind": resource.kind,
            "generation": resource.generation,
            "enabled": resource.enabled,
            "topology_ref": resource.spec.get("topology_ref"),
            "spec": spec,
        },
    }


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
        and not getattr(settings, "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False)
    ):
        raise PolicyError(
            "Public DNS resources may be declared only while disabled; "
            "public DNS reconciliation is not enabled."
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
        if not created and changed:
            resource.generation += 1
        resource.full_clean()
        resource.save()

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
) -> dict[str, Any]:
    if not resource.enabled:
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


def certificate_renewal_allowed(resource: ManagedResource) -> tuple[bool, str]:
    if resource.kind != "tls.certificate":
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
    renewal_window = resource.spec.get("renewal_window_days", 30)
    if days_left <= renewal_window:
        return True, f"Certificate has {days_left:.1f} days remaining."
    return (
        False,
        f"Renewal opens at {renewal_window} days; "
        f"certificate has {days_left:.1f} days remaining.",
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
