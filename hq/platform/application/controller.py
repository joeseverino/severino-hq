"""Lease and report protocol for the privileged homelab controller."""

import hashlib
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, F, JSONField, Q, Value, When
from django.utils import timezone
lazy from django.conf import settings

from hq.domains.control_plane.connection_kinds import CONNECTION_CREDENTIALS
from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND
from hq.domains.control_plane.providers import (
    PROVIDERS,
    controller_capability_registry,
    enabled_controller_actions,
)
from hq.platform.core.attribution import bound
from hq.platform.core.audit import audit_events, audit_operation, audited_labels, record_event
from hq.platform.core.models import AuditLog
lazy from hq.domains.control_plane.provider_adapters.declarations import DELIVERY_TARGET_KIND

from . import agent_access
from .adoption import manages_through, observes_only
from .conditions import stamped
from .infrastructure import controller_contract, resolved_spec, serialize_resource
from .inventory import strike_removed
from .plugin_admission import admitted_sources
from .resource_operations import enqueue, serialize_operation
lazy from .cadence import request_reading, swept
lazy from .dependency_updates import plan as dependency_updates
lazy from .derivations import present
lazy from .expiry import certificate_expiry, renewal_opens_at, renewal_window
lazy from .findings import auto_remediable
lazy from .github_profile import plan as github_profiles
lazy from .github_repositories import plan as github_repositories
lazy from .resource_capabilities import LIFECYCLE_VERBS
lazy from .security import CONTROLLER_INTERFACE, cli_principal


@dataclass(frozen=True, slots=True)
class ControllerReport:
    success: bool
    observed_generation: int
    status: dict[str, Any] = field(default_factory=dict)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    # A run's task outcomes (``RunEvent``); no other operation reports any.
    events: list[dict[str, Any]] = field(default_factory=list)


def _confirm_delivery_targets(resource: ManagedResource, status: dict[str, Any]) -> None:
    """Mark the places a certificate was just verified at as observed.

    No sweep reports a delivery target, so ``confirm_observed`` skips it.
    Reconciling the certificate observes it, matching each target's served
    fingerprint against what HQ issued, a stronger check than any sweep; that
    answer arrives under the certificate's name, so it is recorded here.
    """

    if resource.kind not in (CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND):
        return
    verified = {str(item.get("consumer", "")) for item in (status or {}).get("consumers", ()) if item.get("consumer")}
    if not verified:
        return

    # Joined on the connection each consumer names, never the consumer name,
    # which belongs to the certificate: a target holding a second certificate
    # is named after that one there, so matching by name would confirm the
    # wrong row or nothing.
    reached = {
        consumer.get("connection_ref")
        for consumer in resolved_spec(resource).get("consumers", ())
        if consumer.get("name") in verified and consumer.get("connection_ref")
    }
    if not reached:
        return
    now = timezone.now()
    ManagedResource.objects.filter(kind=DELIVERY_TARGET_KIND, spec__connection_ref__in=reached).update(
        last_observed_at=now, updated_at=now
    )


def _as_its_own(operation: OperationRequest) -> AbstractContextManager[None]:
    """The controller's work on one queued request, bound under the request's
    own id and caused by the operation that queued it, so its claim and report
    read as one operation headed by the request's own rows, and lead back to
    the ask, as an approval's rows run under the approval's id."""

    queued_by = (
        audit_events()
        .filter(
            object_type=audited_labels()[OperationRequest],
            object_id=str(operation.pk),
            action=AuditLog.Action.CREATED,
        )
        .values_list("operation_id", flat=True)
        .first()
    )
    outer = bound()
    return audit_operation(
        operation=outer.operation if outer is not None else "",
        operation_id=str(operation.pk),
        cause=queued_by or "",
    )


# Why a run whose lease lapsed is ended rather than queued again, by the
# retry its manifest declares (``OperationManifest.retry``), pinned in its
# record's spec when it was asked for. ``safe`` alone queues it again.
_UNKNOWN_OUTCOME = {
    "never": "Its lease ran out before it reported, so whether it ran is unknown, and it is never run twice.",
    "observe_first": (
        "Its lease ran out before it reported, so whether it ran is unknown, and its procedure "
        "observes the machines before it is run again."
    ),
}


def _fail_lapsed_runs(now) -> None:
    """End each lapsed run its manifest does not call safe to repeat.

    A run may have changed machines before its worker stopped renewing, and
    HQ cannot tell, so running it again could act twice. Saved one by one
    under the run's own id, so each ending is an audited row that leads back
    to the ask. At most one run per procedure is active, so these are few.
    """

    lapsed = (
        OperationRequest.objects.select_for_update()
        .select_related("resource")
        .filter(state=OperationRequest.State.CLAIMED, lease_expires_at__lte=now, action=OperationRequest.Action.RUN)
        .exclude(resource__spec__retry="safe")
    )
    for operation in lapsed:
        retry = str(operation.resource.spec.get("retry") or "never")
        message = _UNKNOWN_OUTCOME.get(retry, _UNKNOWN_OUTCOME["never"])
        operation.state = OperationRequest.State.FAILED
        operation.completed_at = now
        operation.lease_expires_at = None
        operation.claim_attempt = ""
        operation.result = {"ok": False, "message": message, "reason": "OutcomeUnknown", "retry": retry}
        operation.audit_said = message
        with _as_its_own(operation):
            operation.save(
                update_fields=("state", "completed_at", "lease_expires_at", "claim_attempt", "result", "updated_at")
            )


def _release_lapsed(now) -> None:
    """Put back the work whose lease ran out, or end it if its record is off.

    Switching a record off cancels its queued work (``save_managed_resource``)
    and leaves claimed work to finish. Claimed work whose lease lapses would
    otherwise queue again for a record HQ was told to leave alone, so it ends
    cancelled instead, all but a removal, in the one statement that releases it.
    A run is never put back blindly (``_fail_lapsed_runs``).
    """

    _fail_lapsed_runs(now)
    switched_off = Q(
        resource__in=ManagedResource.objects.filter(enabled=False).values("pk"),
    ) & ~Q(action=OperationRequest.Action.DELETE)
    OperationRequest.objects.filter(
        state=OperationRequest.State.CLAIMED,
        lease_expires_at__lte=now,
    ).update(
        state=Case(
            When(switched_off, then=Value(OperationRequest.State.CANCELLED)),
            default=Value(OperationRequest.State.QUEUED),
        ),
        completed_at=Case(When(switched_off, then=Value(now)), default=None),
        result=Case(
            When(
                switched_off,
                then=Value(
                    {
                        "ok": False,
                        "message": "The record was switched off in HQ, and its lease ran out before it reported.",
                        "reason": "SwitchedOff",
                    },
                    output_field=JSONField(),
                ),
            ),
            default=F("result"),
        ),
        claimed_by="",
        claimed_at=None,
        lease_expires_at=None,
        claim_attempt="",
    )


def _compatible_operations(operations, capabilities: tuple[tuple[str, str], ...]):
    """The operations one of ``capabilities`` performs. A caller naming none
    is offered none: a claim says what it can do, so a worker never takes
    work only another worker has a handler for."""

    if not capabilities:
        return operations.none()
    predicate = Q()
    for kind, action in capabilities:
        predicate |= Q(resource__kind=kind, action=action)
    return operations.filter(predicate)


def _verification(operation: OperationRequest) -> dict[str, int] | None:
    """The verification policy the operation's action declares, if any.

    A certificate reconcile redeploys and verifies under the renewal policy.
    """

    kind = operation.resource.kind
    capability = controller_capability_registry().capabilities.get(kind)
    action = "renew" if kind == CERTIFICATE_KIND else operation.action
    policy = capability.actions.get(action) if capability else None
    verification = policy.verification if policy else None
    if verification is None:
        return None
    return {
        "timeout_seconds": verification.timeout_seconds,
        "interval_seconds": verification.interval_seconds,
    }


def _pending(operation: OperationRequest, *, claimed: bool = False) -> dict[str, Any]:
    pending: dict[str, Any] = {"ok": True, "operation": serialize_operation(operation)}
    verification = _verification(operation)
    if verification is not None:
        pending["verification"] = verification
    # A run's worker learns what to run from its claim alone: the parameters
    # and hosts it was asked and approved for. A peek leases nothing, so it
    # is told none of it.
    if claimed and operation.action == OperationRequest.Action.RUN:
        pending["input"] = operation.input
    return pending


def peek_next_operation(*, capabilities: tuple[tuple[str, str], ...] = ()) -> dict[str, Any]:
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
    result = _pending(operation)
    try:
        result.update(controller_contract(operation.resource))
    except (KeyError, TypeError, ValueError) as exc:
        # Said rather than raised. This is the read-only look at what is next,
        # and a caller asking what the controller would do next is owed the
        # answer "it cannot build this one, because ..." rather than a traceback.
        result["unresolvable"] = str(exc)
    return result


def _certificate_expiry(resource: ManagedResource):
    """The provider's public expiry observation, parsed once, preserving UTC."""

    return certificate_expiry(resource.status)


def _automatic_reconcile(resource: ManagedResource) -> tuple[bool, str, str]:
    not_after = resource.status.get("not_after")
    if resource.kind == CERTIFICATE_KIND and not_after and _certificate_expiry(resource) is None:
        return True, "Automatic: the certificate's expiry could not be read.", "invalid-expiry"
    if resource.generation != resource.observed_generation:
        return True, "Automatic: HQ's config for it changed.", "generation"
    drifted = any(
        item.get("status") is True and item.get("type") in {"Drifted", "Degraded"} for item in resource.conditions
    )
    if drifted:
        # Keyed on what drifted, so a new difference is new work the same day
        # and the same one is acted on once.
        said = "|".join(
            str(item.get("message", "")) for item in resource.conditions if item.get("type") in {"Drifted", "Degraded"}
        )
        digest = hashlib.sha256(said.encode()).hexdigest()[:16]
        return True, "Automatic: it was changed outside HQ.", f"drift-{digest}"
    return False, "", ""


def _automatic_renewal(resource: ManagedResource, now) -> tuple[bool, str, str]:
    if resource.kind != CERTIFICATE_KIND:
        return False, "", ""
    expiry = _certificate_expiry(resource)
    if expiry is None:
        return False, "", ""

    if now >= renewal_opens_at(expiry, renewal_window(resource.spec)):
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

    if not settings.SEVERINO_FINDINGS_AUTO_REMEDY:
        return []

    queued: list[str] = []
    manages = manages_through()
    for repair in auto_remediable(principal=cli_principal()):
        resource = ManagedResource.objects.select_for_update().filter(key=repair.resource_key, enabled=True).first()
        if resource is None or observes_only(resource.kind, resource.spec, manages):
            continue
        # Keyed on the evidence, not the attempt.
        idempotency_key = (f"finding:{repair.rule}:{resource.pk}:g{resource.generation}:{now.date().isoformat()}")[:200]
        _, created = enqueue(
            resource,
            OperationRequest.Action.RECONCILE,
            idempotency_key=idempotency_key,
            requested_actor=controller_id,
            requested_interface=CONTROLLER_INTERFACE,
            reason=repair.reason,
            input={"generation": resource.generation},
        )
        if created:
            queued.append(resource.key)
    return queued


def schedule_automatic_operations(controller_id: str) -> dict[str, Any]:
    """Queue work declared automatic by the validated controller contract."""
    scheduled: list[str] = []
    now = present()
    automatic = enabled_controller_actions(automatic_only=True)
    by_kind: dict[str, list[str]] = {}
    for kind, action in automatic:
        by_kind.setdefault(kind, []).append(action)
    for actions in by_kind.values():
        actions.sort(key=lambda action: action != OperationRequest.Action.RENEW)
    resources = ManagedResource.objects.select_for_update().filter(enabled=True, kind__in=by_kind)
    manages = manages_through()
    for resource in resources:
        if observes_only(resource.kind, resource.spec, manages):
            continue
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
            f"controller:{action}:{resource.pk}:g{resource.generation}:{identity}:{now.date().isoformat()}"
        )[:200]
        operation, created = enqueue(
            resource,
            action,
            idempotency_key=idempotency_key,
            requested_actor=controller_id,
            requested_interface=CONTROLLER_INTERFACE,
            reason=reason,
            input={"generation": resource.generation},
        )
        if created:
            scheduled.append(str(operation.id))
    # The second source, after the contract-driven pass so anything already
    # queued is deduped by the guards above rather than queued twice.
    repaired = _finding_repairs(controller_id, now)
    return {"ok": True, "scheduled": scheduled, "repaired": repaired}


_ATTEMPT_LENGTH = 64


def _check_lease(lease_seconds: int) -> None:
    if not 30 <= lease_seconds <= 3600:
        raise ValueError("Lease duration must be between 30 and 3600 seconds.")


@transaction.atomic
def claim_next_operation(
    controller_id: str,
    *,
    lease_seconds: int = 300,
    capabilities: tuple[tuple[str, str], ...],
    attempt: str = "",
) -> dict[str, Any]:
    """Lease the oldest queued operation one of ``capabilities`` performs.

    ``attempt`` names this claim, so a claim retried after its answer was
    lost is answered the operation it already leased rather than a second
    one, as Amazon SQS's receive attempt id does. A run whose lease lapses
    ends rather than queuing again, so a lost answer would otherwise fail a
    run nothing ever started.
    """

    _check_lease(lease_seconds)
    if not controller_id:
        raise ValueError("A claim names the controller making it.")
    if len(attempt) > _ATTEMPT_LENGTH:
        raise ValueError(f"A claim attempt is at most {_ATTEMPT_LENGTH} characters.")
    now = timezone.now()
    _release_lapsed(now)
    if attempt:
        held = (
            OperationRequest.objects.select_related("resource")
            .filter(
                state=OperationRequest.State.CLAIMED,
                claimed_by=controller_id,
                claim_attempt=attempt,
                lease_expires_at__gt=now,
            )
            .first()
        )
        if held is not None:
            result = _pending(held, claimed=True)
            result.update(controller_contract(held.resource))
            return result
    operations = (
        OperationRequest.objects.select_for_update(skip_locked=True)
        .select_related("resource")
        .filter(state=OperationRequest.State.QUEUED)
        .order_by("created_at")
    )
    # The pause and a block stop what an agent already asked for, not only
    # what it asks next. Left queued, so lifting the brake resumes it.
    operations = operations.exclude(agent_access.held_askers())
    operations = _compatible_operations(operations, capabilities)
    operation, contract = _next_resolvable(operations, now)
    if operation is None:
        return {"ok": True, "operation": None}
    operation.state = OperationRequest.State.CLAIMED
    operation.claimed_by = controller_id
    operation.claimed_at = now
    operation.lease_expires_at = now + timedelta(seconds=lease_seconds)
    operation.claim_attempt = attempt
    operation.attempt_count += 1
    with _as_its_own(operation):
        operation.save(
            update_fields=(
                "state",
                "claimed_by",
                "claimed_at",
                "lease_expires_at",
                "claim_attempt",
                "attempt_count",
                "updated_at",
            )
        )
    result = _pending(operation, claimed=True)
    result.update(contract)
    return result


def _next_resolvable(operations, now):
    """The first queued operation HQ can actually describe, failing the rest.

    A resource whose spec cannot be resolved is not work waiting to happen; it
    is work that cannot be done, and saying so is the only useful thing left.
    Raising would roll back the claim and leave the operation at the head of a
    queue ordered by age, so every later poll would hit it and nothing behind
    it would ever be claimed.
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
            operation.audit_said = str(exc)
            with _as_its_own(operation):
                operation.save(update_fields=("state", "completed_at", "result", "updated_at"))
    return None, {}


def _stop(reason: str, lease_expires_at=None) -> dict[str, Any]:
    return {
        "ok": True,
        "continue": False,
        "reason": reason,
        "lease_expires_at": lease_expires_at.isoformat() if lease_expires_at else None,
    }


@transaction.atomic
def renew_operation(operation_id: str, *, controller_id: str, lease_seconds: int = 300) -> dict[str, Any]:
    """Extend the lease the claimant holds, and say whether it may go on.

    A worker running long work renews while it runs, so its lease lapses only
    when it stops renewing. Only the claimant renews, and only before its
    lease runs out: a lapsed lease is released by the next claim, and a run's
    outcome is then unknown (``_fail_lapsed_runs``). ``continue`` is false
    once its asker is held by the pause or a block, or the operation was
    cancelled, and the worker stops. A held lease is still extended, so the
    worker has time to stop and report what it did.

    The renewal itself is lease bookkeeping, written with no audit row: the
    claim and the outcome are the operation's events, and a row per renewal
    would bury them.
    """

    _check_lease(lease_seconds)
    if not controller_id:
        raise ValueError("A renewal names the controller making it.")
    now = timezone.now()
    try:
        operation = OperationRequest.objects.select_for_update().get(pk=operation_id)
    except (OperationRequest.DoesNotExist, ValueError, ValidationError) as exc:
        raise ValueError(f"Operation {operation_id!r} was not found.") from exc
    if operation.claimed_by and operation.claimed_by != controller_id:
        raise ValueError("Operation is leased by a different controller.")
    if operation.state == OperationRequest.State.CANCELLED:
        return _stop("Cancelled")
    if operation.state != OperationRequest.State.CLAIMED:
        raise ValueError(f"Operation is {operation.state!r}, not claimed.")
    if operation.lease_expires_at is None or operation.lease_expires_at <= now:
        raise ValueError("Operation lease has expired.")
    expires = now + timedelta(seconds=lease_seconds)
    OperationRequest.objects.filter(pk=operation.pk).update(lease_expires_at=expires, updated_at=now)
    held = OperationRequest.objects.filter(pk=operation.pk).filter(agent_access.held_askers()).exists()
    if held:
        return _stop("Held", expires)
    return {"ok": True, "continue": True, "reason": "", "lease_expires_at": expires.isoformat()}


@transaction.atomic
def _read_back_lifecycle(operation: OperationRequest) -> None:
    """Ask for a lifecycle operation's kind to be read once it is reported
    done, whatever its outcome. The ask made when it was queued can be
    answered by a sweep already running then, which read the state before
    the operation acted."""

    kind = operation.resource.kind
    if operation.action in LIFECYCLE_VERBS and swept(kind):
        transaction.on_commit(lambda: request_reading(kind))


def report_operation(
    operation_id: str,
    report: ControllerReport,
    *,
    controller_id: str,
) -> dict[str, Any]:
    # Held to the bridge's secret door (``secret_shapes``) before it arrives.
    try:
        operation = OperationRequest.objects.select_for_update().select_related("resource").get(pk=operation_id)
    except (OperationRequest.DoesNotExist, ValueError, ValidationError) as exc:
        raise ValueError(f"Operation {operation_id!r} was not found.") from exc
    if operation.state != OperationRequest.State.CLAIMED:
        raise ValueError(f"Operation is {operation.state!r}, not claimed.")
    if operation.claimed_by != controller_id:
        raise ValueError("Operation is leased by a different controller.")
    if operation.lease_expires_at and operation.lease_expires_at <= timezone.now():
        raise ValueError("Operation lease has expired.")

    requested_generation = operation.input.get("generation")
    if report.observed_generation != requested_generation:
        raise ValueError("Report generation does not match the operation's requested generation.")
    run = operation.action == OperationRequest.Action.RUN
    if run and report.status:
        raise ValueError("A run reports what its tasks did as events, never a status for its record.")
    if report.events and not run:
        raise ValueError("Only a run reports events.")

    with _as_its_own(operation):
        if run:
            return _settle_run(operation, report)
        return _settle_report(operation, report)


def _finish(operation: OperationRequest, report: ControllerReport, result: dict[str, Any]) -> None:
    operation.result = result
    operation.completed_at = timezone.now()
    operation.lease_expires_at = None
    operation.claim_attempt = ""
    operation.state = OperationRequest.State.SUCCEEDED if report.success else OperationRequest.State.FAILED
    # The finishing row says what the controller said.
    operation.audit_said = report.message
    operation.save(
        update_fields=(
            "result",
            "completed_at",
            "lease_expires_at",
            "claim_attempt",
            "state",
            "updated_at",
        )
    )


# The task outcomes a run's audit trail records one row each: what changed a
# machine or went wrong. Every outcome stays in the run's result.
_AUDITED_OUTCOMES = {
    "changed": AuditLog.Action.DONE,
    "failed": AuditLog.Action.FAILED,
    "unreachable": AuditLog.Action.FAILED,
}


def _settle_run(operation: OperationRequest, report: ControllerReport) -> dict[str, Any]:
    """Record how a run ended, as evidence on the run alone.

    A procedure's record holds the manifest it is pinned to, never what a run
    found, so nothing here writes it. The events arrive bounded by the bridge
    contract (``RunEvent``) and struck of secrets at its door
    (``secret_shapes``).
    """

    events = [dict(event) for event in report.events]
    _finish(
        operation,
        report,
        {"message": report.message, "conditions": report.conditions, "events": events},
    )
    for event in events:
        action = _AUDITED_OUTCOMES.get(event["outcome"])
        if action is None:
            continue
        record_event(
            action=action,
            obj=operation,
            type_label=audited_labels()[OperationRequest],
            message=f"{event['host']}: {event['task']} {event['outcome']}",
            metadata={"event": {name: event[name] for name in ("host", "task", "outcome")}},
            required=True,
        )
    return {"ok": True, "operation": serialize_operation(operation)}


def _settle_report(operation: OperationRequest, report: ControllerReport) -> dict[str, Any]:
    """Record what the controller reports of ``operation`` and of its resource."""

    resource = operation.resource
    _finish(
        operation,
        report,
        {"message": report.message, "status": report.status, "conditions": report.conditions},
    )

    resource.conditions = stamped(resource.conditions, report.conditions)
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
    if report.success:
        _confirm_delivery_targets(resource, report.status)
    _read_back_lifecycle(operation)

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
        # is unaffected: it is written separately, and outlives both.
        answer = {
            "ok": True,
            "operation": serialize_operation(operation),
            "resource": serialize_resource(resource),
            "removed": True,
        }
        OperationRequest.objects.filter(resource=resource).delete()
        # The stored list still holds the record: struck here, where HQ learns
        # it is gone, so no reader of a stored list takes it back on.
        strike_removed(resource.kind, resource.spec)
        resource.delete()
        return answer

    return {
        "ok": True,
        "operation": serialize_operation(operation),
        "resource": serialize_resource(resource),
    }


def controller_registry() -> dict[str, Any]:
    """The provider declarations a controller acts on, as data.

    A controller that cannot import these declarations asks for them: which
    actions it may apply, which it must refuse and why, which kinds need material
    HQ holds, which connections can read each kind, which extensions the image
    composes, whose public GitHub profile is due a read, which GitHub
    repositories HQ holds and which are due a full read, and which repositories
    a dependency update run covers and whether one is due.
    """

    registry = controller_capability_registry()
    return {
        "ok": True,
        "capabilities": [{"kind": kind, "action": action} for kind, action in enabled_controller_actions()],
        "locked": [
            {"kind": kind, "action": action, "reason": policy.reason}
            for kind, capability in sorted(registry.capabilities.items())
            for action, policy in sorted(capability.actions.items())
            if policy.mode == "locked"
        ],
        "material_kinds": sorted(kind for kind, provider in PROVIDERS.items() if provider.material_capability),
        "connection_providers": {
            kind: list(provider.connection_providers) for kind, provider in sorted(PROVIDERS.items())
        },
        "observations": {kind: reading.provider for kind, reading in sorted(OBSERVATIONS.items())},
        "connection_credentials": sorted(CONNECTION_CREDENTIALS),
        "extensions": [dict(source) for source in admitted_sources()],
        "github_profiles": github_profiles(),
        "github_repositories": github_repositories(),
        "dependency_updates": dependency_updates(),
    }
