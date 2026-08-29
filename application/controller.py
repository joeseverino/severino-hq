"""Lease and report protocol for the privileged homelab controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, timezone as datetime_timezone
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from control_plane.models import ManagedResource, OperationRequest
from control_plane.providers import CERTIFICATE_KIND, enabled_controller_actions

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
    try:
        result.update(controller_contract(operation.resource))
    except (KeyError, TypeError, ValueError) as exc:
        # Said rather than raised. This is the read-only look at what is next,
        # and a caller asking what the controller would do next is owed the
        # answer "it cannot build this one, because ..." rather than a traceback.
        result["unresolvable"] = str(exc)
    return result


def _scheduler_now():
    return timezone.now()


def _certificate_expiry(resource: ManagedResource):
    """Parse the provider's public expiry observation once, preserving UTC."""

    not_after = resource.status.get("not_after")
    if not not_after:
        return None
    try:
        expiry = timezone.datetime.fromisoformat(not_after.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime_timezone.utc)
    return expiry


def _automatic_reconcile(resource: ManagedResource) -> tuple[bool, str, str]:
    not_after = resource.status.get("not_after")
    if resource.kind == CERTIFICATE_KIND and not_after and _certificate_expiry(resource) is None:
        return True, "Automatic repair of invalid certificate observation.", "invalid-expiry"
    if resource.generation != resource.observed_generation:
        return True, "Automatic reconciliation of a new desired generation.", "generation"
    drifted = any(
        item.get("status") is True
        and item.get("type") in {"Drifted", "Degraded"}
        for item in resource.conditions
    )
    if drifted:
        return True, "Automatic reconciliation of provider drift.", "drift"
    return False, "", ""


def _automatic_renewal(resource: ManagedResource, now) -> tuple[bool, str, str]:
    if resource.kind != CERTIFICATE_KIND:
        return False, "", ""
    expiry = _certificate_expiry(resource)
    if expiry is None:
        return False, "", ""
    renewal_at = expiry - timedelta(days=resource.spec.get("renewal_window_days", 30))
    if now >= renewal_at:
        return True, "Automatic renewal window reached.", resource.status["not_after"]
    return False, "", ""


def _automatic_action(
    resource: ManagedResource,
    action: str,
    now,
) -> tuple[bool, str, str]:
    """Evaluate one declared automatic action without executing it."""

    if action == OperationRequest.Action.RECONCILE:
        return _automatic_reconcile(resource)
    if action == OperationRequest.Action.RENEW:
        return _automatic_renewal(resource, now)
    return False, "", ""


@transaction.atomic
def _finding_repairs(controller_id: str, now) -> list[str]:
    """Queue repairs for findings the controller contract already runs itself.

    A second source of due work beside `_automatic_action`, which cannot see
    this class of fault: it asks "has the declaration changed, or is something
    reporting broken?" and the failure it misses is the one where nothing
    changed and nothing looked broken.
    """

    from django.conf import settings

    if not getattr(settings, "SEVERINO_FINDINGS_AUTO_REMEDY", False):
        return []
    from .findings import auto_remediable
    from .security import cli_principal

    queued: list[str] = []
    for repair in auto_remediable(principal=cli_principal()):
        resource = ManagedResource.objects.select_for_update().filter(
            key=repair.resource_key, enabled=True
        ).first()
        if resource is None:
            continue
        # Keyed on the evidence, not the attempt.
        idempotency_key = (
            f"finding:{repair.rule}:{resource.pk}:g{resource.generation}:"
            f"{now.date().isoformat()}"
        )[:200]
        if OperationRequest.objects.filter(idempotency_key=idempotency_key).exists():
            continue
        if OperationRequest.objects.filter(
            resource=resource,
            action=OperationRequest.Action.RECONCILE,
            state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED),
        ).exists():
            continue
        OperationRequest.objects.create(
            resource=resource,
            action=OperationRequest.Action.RECONCILE,
            requested_actor=controller_id,
            requested_interface="controller",
            reason=repair.reason,
            idempotency_key=idempotency_key,
            input={"generation": resource.generation},
        )
        queued.append(resource.key)
    return queued


def schedule_automatic_operations(controller_id: str) -> dict[str, Any]:
    """Queue work declared automatic by the validated controller contract."""
    scheduled: list[str] = []
    now = _scheduler_now()
    automatic = enabled_controller_actions(automatic_only=True)
    by_kind: dict[str, list[str]] = {}
    for kind, action in automatic:
        by_kind.setdefault(kind, []).append(action)
    for actions in by_kind.values():
        actions.sort(key=lambda action: action != OperationRequest.Action.RENEW)
    resources = ManagedResource.objects.select_for_update().filter(
        enabled=True, kind__in=by_kind
    )
    for resource in resources:
        selected = next(
            (
                (action, reason, identity)
                for action in by_kind[resource.kind]
                for due, reason, identity in [_automatic_action(resource, action, now)]
                if due
            ),
            None,
        )
        if selected is None:
            continue
        action, reason, identity = selected
        idempotency_key = (
            f"controller:{action}:{resource.pk}:g{resource.generation}:"
            f"{identity}:{now.date().isoformat()}"
        )[:200]
        if OperationRequest.objects.filter(idempotency_key=idempotency_key).exists():
            continue
        if OperationRequest.objects.filter(
            resource=resource,
            action=action,
            state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED),
        ).exists():
            continue
        operation = OperationRequest.objects.create(
            resource=resource,
            action=action,
            requested_actor=controller_id,
            requested_interface="controller",
            reason=reason,
            idempotency_key=idempotency_key,
            input={"generation": resource.generation},
        )
        scheduled.append(str(operation.id))
    # The second source, after the contract-driven pass so anything already
    # queued is deduped by the guards above rather than queued twice.
    repaired = _finding_repairs(controller_id, now)
    return {"ok": True, "scheduled": scheduled, "repaired": repaired}


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
    operation, contract = _next_resolvable(operations, now)
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
    result.update(contract)
    return result


def _next_resolvable(operations, now):
    """The first queued operation HQ can actually describe, failing the rest.

    A resource whose spec cannot be resolved is not work waiting to happen; it
    is work that cannot be done, and saying so is the only useful thing left.
    Raised instead, it rolled back the claim and left the operation at the head
    of a queue ordered by age -- so every poll after it hit the same one and
    nothing else was ever claimed. A certificate naming a target that has been
    removed stopped DNS, proxies and renewals, silently, everywhere.
    """

    for operation in operations:
        try:
            return operation, controller_contract(operation.resource)
        except (KeyError, TypeError, ValueError) as exc:
            operation.state = OperationRequest.State.FAILED
            operation.completed_at = now
            operation.result = {
                "ok": False,
                "message": str(exc),
                "reason": "Unresolvable",
            }
            operation.save(
                update_fields=("state", "completed_at", "result", "updated_at")
            )
    return None, {}


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

    if report.success and operation.action == OperationRequest.Action.DELETE:
        # The declaration outlived the thing it described. Keeping it would put
        # a row on the board for something that is no longer anywhere, and the
        # next reconcile would recreate what was just removed.
        #
        # Serialized before the row goes, because the caller is owed the same
        # answer shape for a delete as for anything else. The operations are
        # removed explicitly rather than by loosening the foreign key: PROTECT
        # is what stops an accidental delete elsewhere taking history with it,
        # and this is the one place the removal is deliberate. The audit trail
        # is unaffected -- it is written separately, and outlives both.
        answer = {
            "ok": True,
            "operation": serialize_operation(operation),
            "resource": serialize_resource(resource),
            "removed": True,
        }
        OperationRequest.objects.filter(resource=resource).delete()
        resource.delete()
        return answer

    return {
        "ok": True,
        "operation": serialize_operation(operation),
        "resource": serialize_resource(resource),
    }
