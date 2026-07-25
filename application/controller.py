"""Lease and report protocol for the privileged homelab controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from control_plane.models import OperationRequest

from .infrastructure import controller_contract, serialize_operation, serialize_resource

_FORBIDDEN_STATUS_KEYS = ("private", "secret", "token", "password", "credential")


@dataclass(frozen=True)
class ControllerReport:
    success: bool
    observed_generation: int
    status: dict[str, Any] = field(default_factory=dict)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


def _assert_public_status(value: Any, path: str = "status") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in _FORBIDDEN_STATUS_KEYS):
                raise ValueError(f"{path}.{key} is secret-bearing and cannot enter HQ.")
            _assert_public_status(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_public_status(child, f"{path}[{index}]")
    elif isinstance(value, str) and "PRIVATE KEY-----" in value:
        raise ValueError(f"{path} contains private-key material.")


def _compatible_operations(operations, capabilities: tuple[tuple[str, str], ...]):
    if not capabilities:
        return operations
    predicate = Q()
    for kind, action in capabilities:
        predicate |= Q(resource__kind=kind, action=action)
    return operations.filter(predicate)


def peek_next_operation(
    *, capabilities: tuple[tuple[str, str], ...] = ()
) -> dict[str, Any]:
    """Read the next compatible queued operation without leasing it."""
    operations = (
        OperationRequest.objects.select_related("resource")
        .filter(state=OperationRequest.State.QUEUED)
        .order_by("created_at")
    )
    operations = _compatible_operations(operations, capabilities)
    operation = operations.first()
    if operation is None:
        return {"ok": True, "operation": None}
    result = {"ok": True, "operation": serialize_operation(operation)}
    result.update(controller_contract(operation.resource))
    return result


@transaction.atomic
def claim_next_operation(
    controller_id: str,
    *,
    lease_seconds: int = 300,
    capabilities: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    if not 30 <= lease_seconds <= 3600:
        raise ValueError("Lease duration must be between 30 and 3600 seconds.")
    now = timezone.now()
    OperationRequest.objects.filter(
        state=OperationRequest.State.CLAIMED,
        lease_expires_at__lte=now,
    ).update(
        state=OperationRequest.State.QUEUED,
        claimed_by="",
        claimed_at=None,
        lease_expires_at=None,
    )
    operations = (
        OperationRequest.objects.select_for_update(skip_locked=True)
        .select_related("resource")
        .filter(state=OperationRequest.State.QUEUED)
        .order_by("created_at")
    )
    operations = _compatible_operations(operations, capabilities)
    operation = operations.first()
    if operation is None:
        return {"ok": True, "operation": None}
    operation.state = OperationRequest.State.CLAIMED
    operation.claimed_by = controller_id
    operation.claimed_at = now
    operation.lease_expires_at = now + timedelta(seconds=lease_seconds)
    operation.attempt_count += 1
    operation.save(
        update_fields=(
            "state",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "attempt_count",
            "updated_at",
        )
    )
    result = {
        "ok": True,
        "operation": serialize_operation(operation),
    }
    result.update(controller_contract(operation.resource))
    return result


@transaction.atomic
def report_operation(
    operation_id: str,
    report: ControllerReport,
    *,
    controller_id: str,
) -> dict[str, Any]:
    _assert_public_status(report.status)
    _assert_public_status(report.conditions, "conditions")
    try:
        operation = (
            OperationRequest.objects.select_for_update()
            .select_related("resource")
            .get(pk=operation_id)
        )
    except (OperationRequest.DoesNotExist, ValueError) as exc:
        raise ValueError(f"Operation {operation_id!r} was not found.") from exc
    if operation.state != OperationRequest.State.CLAIMED:
        raise ValueError(f"Operation is {operation.state!r}, not claimed.")
    if operation.claimed_by != controller_id:
        raise ValueError("Operation is leased by a different controller.")
    if operation.lease_expires_at and operation.lease_expires_at <= timezone.now():
        raise ValueError("Operation lease has expired.")

    resource = operation.resource
    requested_generation = operation.input.get("generation")
    if report.observed_generation != requested_generation:
        raise ValueError(
            "Report generation does not match the operation's requested generation."
        )

    operation.result = {
        "message": report.message,
        "status": report.status,
        "conditions": report.conditions,
    }
    operation.completed_at = timezone.now()
    operation.lease_expires_at = None
    operation.state = (
        OperationRequest.State.SUCCEEDED
        if report.success
        else OperationRequest.State.FAILED
    )
    operation.save(
        update_fields=(
            "result",
            "completed_at",
            "lease_expires_at",
            "state",
            "updated_at",
        )
    )

    resource.conditions = report.conditions
    if report.success:
        resource.status = report.status
        resource.last_observed_at = timezone.now()
        resource.observed_generation = report.observed_generation
        resource_fields = (
            "status",
            "conditions",
            "last_observed_at",
            "observed_generation",
            "updated_at",
        )
    else:
        resource_fields = ("conditions", "updated_at")
    resource.save(update_fields=resource_fields)
    return {
        "ok": True,
        "operation": serialize_operation(operation),
        "resource": serialize_resource(resource),
    }
