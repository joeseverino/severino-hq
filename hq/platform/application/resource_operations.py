"""Operations queued against a declared resource.

Reconcile, lifecycle, removal, route approval, reach and certificate renewal:
each is checked against the provider's policy, queued atomically and audited
before the controller runs it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.db import transaction

from hq.domains.control_plane.desired_state import advance_dependents
from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND
from hq.domains.control_plane.providers import PROVIDERS, controller_action_policy
from hq.platform.core.audit import operation_context

from .adoption import OBSERVES_ONLY, observes_only
from .approvals import consent_gap
from .cadence import ring_doorbell
from .expiry import certificate_expiry, days_until, renewal_opens_at, renewal_window
from .infrastructure import (
    ManagedResourceCommand,
    NotFoundError,
    PolicyError,
    delivery_targets,
    is_drifted,
    save_managed_resource,
)
from .security import Capability, Principal
from .ui import counted


@dataclass(frozen=True, slots=True)
class OperationCommand:
    idempotency_key: str
    reason: str = ""


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
        "completed_at": (operation.completed_at.isoformat() if operation.completed_at else None),
        "claimed_by": operation.claimed_by,
        "lease_expires_at": (operation.lease_expires_at.isoformat() if operation.lease_expires_at else None),
        "attempt_count": operation.attempt_count,
        "result": operation.result,
    }


# What each action is called in a row of history.
ACTION_LABELS = {
    OperationRequest.Action.RECONCILE: "Apply HQ's settings",
    OperationRequest.Action.RENEW: "Renew certificate",
    OperationRequest.Action.DELETE: "Remove",
    OperationRequest.Action.RESTART: "Restart",
    OperationRequest.Action.START: "Start",
    OperationRequest.Action.STOP: "Stop",
    OperationRequest.Action.APPROVE_ROUTES: "Approve routes",
}
# Conditions that say something is wrong, whatever the operation's own state.
_PROBLEM_CONDITIONS = ("Degraded", "Drifted")
_AUTOMATIC = "controller"
# How many past operations are read to find the ones that changed something.
HISTORY_WINDOW = 200


def _active(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in result.get("conditions") or [] if item.get("status") is True]


def _problem(result: dict[str, Any]) -> dict[str, Any] | None:
    """The condition saying something is wrong, which a result can carry while succeeding."""

    return next((item for item in _active(result) if item.get("type") in _PROBLEM_CONDITIONS), None)


def _outcome(operation: OperationRequest) -> tuple[str, str, str]:
    """``(tone, label, headline)``: how one operation ended, in a pill and a sentence.

    Read from the result's condition before the operation's state, so a run
    that finished and found a problem never reads as done.
    """

    result = operation.result or {}
    active = _active(result)
    said = result.get("message") or operation.reason
    if operation.state == OperationRequest.State.QUEUED:
        return "paused", "Waiting", "Waiting for the controller."
    if operation.state == OperationRequest.State.CLAIMED:
        return "active", "Running", "The controller is working on it."
    if operation.state == OperationRequest.State.FAILED:
        headline = (active[0].get("message") if active else "") or said or "It failed."
        return "serious", "Failed", headline
    problem = _problem(result)
    if problem is not None:
        return "attention", "Problem found", problem.get("message") or said or "It found a problem."
    return "good", "Done", said or "Done."


def requested_by(operation: OperationRequest) -> str:
    """Who asked, as a person reads it."""

    from .approvals import AGENT_SURFACES

    if operation.requested_interface == _AUTOMATIC:
        return "Automatic"
    if operation.requested_interface in AGENT_SURFACES:
        return f"{operation.requested_actor} (agent)"
    return operation.requested_actor


def operation_summary(operation: OperationRequest) -> dict[str, Any]:
    """Project one operation into one sentence and its structured evidence."""
    result = operation.result or {}
    status = result.get("status") or {}
    evidence = status.get("consumers") or []
    affected = [item for item in evidence if item.get("matches_expected") is False]
    tone, label, headline = _outcome(operation)

    return {
        "id": str(operation.id),
        "action": operation.action,
        "action_label": ACTION_LABELS.get(operation.action, operation.get_action_display()),
        "state": operation.state,
        "state_label": label,
        "tone": tone,
        "headline": headline,
        "automatic": operation.requested_interface == _AUTOMATIC,
        "requested_actor": operation.requested_actor,
        "requested_interface": operation.requested_interface,
        "by": requested_by(operation),
        # Blank for almost everything, and the point when it is not: this is the
        # person who agreed to a change a credential asked for.
        "approved_by": (operation.input or {}).get("approved_by", ""),
        "created_at": operation.created_at.isoformat(),
        "completed_at": (operation.completed_at.isoformat() if operation.completed_at else None),
        "attempt_count": operation.attempt_count,
        "reason": operation.reason,
        "condition": next(iter(_active(result)), None),
        "affected": affected,
        "evidence": evidence,
        "matched": sum(1 for item in evidence if item.get("matches_expected") is True),
        "expected_fingerprint_sha256": status.get("expected_fingerprint_sha256", ""),
        # The certificate itself is on the record's page; a history row is not a second copy.
        "raw_result": {
            **result,
            **({"status": {k: v for k, v in status.items() if k != "certificate_pem"}} if status else {}),
        },
    }


def changes(operations: Any, limit: int) -> list[OperationRequest]:
    """The operations worth a row of history, newest first.

    ``operations`` is newest first. An automatic run that ended exactly as the
    run before it on the same record (same state, same sentence, same
    conditions) repeats what is already listed, so it is left out: a check
    that runs every minute is one row until its answer changes. Anything a
    person or an agent asked for, anything unfinished and anything that failed
    is always listed.
    """

    def signature(operation: OperationRequest) -> tuple[Any, ...]:
        result = operation.result or {}
        return (
            operation.state,
            result.get("message", ""),
            tuple((item.get("type"), item.get("reason"), item.get("message")) for item in _active(result)),
        )

    last: dict[tuple[Any, str], tuple[Any, ...]] = {}
    kept: list[OperationRequest] = []
    for operation in reversed(list(operations)):
        key = (operation.resource_id, operation.action)
        said = signature(operation)
        repeats = (
            operation.requested_interface == _AUTOMATIC
            and operation.state == OperationRequest.State.SUCCEEDED
            and last.get(key) == said
        )
        last[key] = said
        if not repeats:
            kept.append(operation)
    kept.reverse()
    return kept[:limit]


def resource_history(resource: ManagedResource, limit: int = 20) -> list[dict[str, Any]]:
    """One record's history: each operation that changed something, newest first."""

    return [operation_summary(operation) for operation in changes(resource.operations.all()[:HISTORY_WINDOW], limit)]


def refuse_while_drifted(resource: ManagedResource) -> None:
    """Refuse to amend a declaration the provider no longer matches.

    A remedy that edits a declaration edits HQ's copy, and applying it pushes
    that whole copy. While the live record differs, whatever changed there
    would be overwritten along with the one intended change, so the operator
    decides first: keep the live version, or restore HQ's.
    """

    if is_drifted(resource):
        raise PolicyError(
            f"{resource.key} differs from what is live. Keep the live version or "
            "restore HQ's first, so this change is made to what is actually in force."
        )


def _resource_for_operation(key: str) -> ManagedResource:
    try:
        return ManagedResource.objects.select_for_update().get(key=key)
    except ManagedResource.DoesNotExist as exc:
        raise NotFoundError(f"No record named {key!r}.") from exc


def _queue_operation(
    resource: ManagedResource,
    command: OperationCommand,
    *,
    principal: Principal,
    action: str,
    require_enabled: bool = True,
) -> dict[str, Any]:
    if require_enabled and not resource.enabled:
        raise PolicyError(f"{resource.key} is switched off in HQ.")
    if observes_only(resource.kind, resource.spec):
        raise PolicyError(OBSERVES_ONLY)
    # Nothing enters the queue for a gated kind without a person behind it. The
    # controller's own automatic work does not pass through here (it writes its
    # rows directly, as itself, converging toward a declaration somebody has
    # already agreed to) so this refuses exactly the case it is about: a
    # credential asking for the world to be changed.
    gap = consent_gap(resource.kind, principal=principal)
    if gap:
        raise PolicyError(gap)
    allowed, explanation = controller_action_policy(resource.kind, action)
    if not allowed:
        raise PolicyError(explanation)
    existing = OperationRequest.objects.filter(idempotency_key=command.idempotency_key).first()
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
        # Who asked stays who asked. Where a person had to agree before this
        # could be queued, that is recorded beside the request rather than in
        # place of it: an operation applied on somebody's say-so and one a
        # credential queued alone must not read the same afterwards.
        input=(
            {"generation": resource.generation, "approved_by": principal.approved_by}
            if principal.approved_by
            else {"generation": resource.generation}
        ),
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
    from .resource_capabilities import public_dns_enabled

    if PROVIDERS[resource.kind].public_effect and not public_dns_enabled():
        raise PolicyError("Public DNS changes are off on this server.")
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
        ManagedResource.objects.filter(kind=kind, **{f"spec__{their_field}__iexact": value}).values_list(
            "key", flat=True
        )
    )


@transaction.atomic
def _forget_declaration(
    resource: ManagedResource, command: OperationCommand, *, principal: Principal
) -> dict[str, Any]:
    """Stop being responsible for something without touching the provider.

    For a declaration-only kind (a domain) and for anything whose connection
    only observes. Nothing is queued and nothing is deleted at the provider.

    The declarations *inside* it go too. Left behind, HQ would keep reconciling
    records in a domain it is no longer responsible for: still writing to a
    zone the operator had just said was not its business, which is the one
    outcome this has to avoid. They are forgotten rather than deleted: the
    records stay exactly as they are at the provider, which is what stepping
    back means.
    """

    from .adoption import keep_out_declaration

    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="infrastructure.resource.forget",
    ):
        contained = _contained_keys(resource)
        ManagedResource.objects.filter(key__in=contained).delete()
        key = resource.key
        kind = resource.kind
        # The next sweep would adopt it again. The operator's choice is kept
        # until they manage it again.
        keep_out_declaration(kind, resource.spec, principal=principal)
        resource.delete()
        # Removing one changes what others resolve to as much as editing one,
        # so dependents advance here too.
        provider = PROVIDERS.get(kind)
        if provider is not None and provider.resolution_input:
            advance_dependents(delivery_targets())
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
    declaration (it is a thing asked for once, about something already exactly
    as declared) so it neither bumps the generation nor waits on one.

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
        return _queue_operation(resource, command, principal=principal, action=action)


@transaction.atomic
def accept_observed(
    command: OperationCommand,
    *,
    principal: Principal,
    current_key: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Make the declaration say what the provider holds.

    For a change made at the provider on purpose: a policy a connector edited,
    a setting changed in its own console. Reconciling would undo it; this
    keeps it, by copying the live record into the declaration, the same way
    adopting does. Fields no sweep can observe (hidden, on demand) stay as
    they were declared. Nothing is written to the provider, and the next sweep
    finds the two agreeing.
    """

    from .inventory import live_spec

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = _resource_for_operation(current_key)
    found = live_spec(resource.kind, resource.spec)
    if found is None:
        raise NotFoundError(f"No live record was last seen for {resource.key!r}, so there is nothing to accept.")
    kept = {
        field: resource.spec[field] for field in PROVIDERS[resource.kind].unobservable_fields if field in resource.spec
    }
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="infrastructure.resource.accept_observed",
    ):
        result = save_managed_resource(
            ManagedResourceCommand(
                key=resource.key, kind=resource.kind, spec={**found, **kept}, enabled=resource.enabled
            ),
            principal=principal,
            current_key=resource.key,
            copied_from_live=True,
        )
    return {**result, "reason": command.reason}


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
    rather than remove it, and nothing would be left pointing at the orphan.
    HQ drops its own row only once a controller reports the provider is clear.

    Removal is queued even for a disabled resource: disabling stops HQ
    reconciling a declaration, which is exactly the state something is left in
    just before an operator decides to be rid of it.
    """

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = _resource_for_operation(current_key)
    # HQ deletes at the provider only through a connection that manages.
    if PROVIDERS[resource.kind].declaration_only or observes_only(resource.kind, resource.spec):
        return _forget_declaration(resource, command, principal=principal)
    allowed, explanation = controller_action_policy(resource.kind, OperationRequest.Action.DELETE)
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
        return False, "Only a certificate HQ issues can be renewed."
    if not resource.enabled:
        return False, "The certificate is switched off in HQ."
    allowed, explanation = controller_action_policy(resource.kind, OperationRequest.Action.RENEW)
    if not allowed:
        return False, explanation
    if any(
        condition.get("status") is True and condition.get("type") in {"Drifted", "Degraded"}
        for condition in resource.conditions
    ):
        return True, "A place it is installed is serving a different certificate or has a problem."

    if not resource.status.get("not_after"):
        return True, "HQ has not read when it expires."
    expiry = certificate_expiry(resource.status)
    if expiry is None:
        return True, "The expiry HQ read is not a date."
    window = renewal_window(resource.spec)
    left = max(0, days_until(expiry))
    if datetime.now(UTC) >= renewal_opens_at(expiry, window):
        return True, f"{counted(left, 'day')} remaining."
    from .moments import when_day

    return (
        False,
        f"Renews automatically from {when_day(renewal_opens_at(expiry, window))} "
        f"({counted(window, 'day')} before it expires).",
    )


@transaction.atomic
def request_route_approval(
    command: OperationCommand,
    *,
    principal: Principal,
    current_key: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Approve the routes a tailnet device is already advertising.

    The remedy the "advertises routes nothing approved" finding names. HQ does
    not choose the routes: the machine has already said what it offers, and
    this is the consent that turns that offer into something the tailnet hands
    out. Which is why it is never automatic: approving a route is a decision
    to trust that machine with traffic for those addresses.
    """

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = _resource_for_operation(current_key)
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="tailnet.routes.approve.request",
    ):
        return _queue_operation(
            resource,
            command,
            principal=principal,
            action=OperationRequest.Action.APPROVE_ROUTES,
        )


def request_reach_allow(
    command: OperationCommand,
    *,
    principal: Principal,
    current_key: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Open the path a reading proved is shut, where the tailnet is what shut it.

    Given a resource, never a policy. Which path to open is re-derived here
    from what the controller observed (the address and port it could not
    reach) so a caller cannot name one. The only change this can ever produce
    is the one an observation already justified.

    The amendment is written to the policy declaration through the ordinary
    write, which is what puts it in front of a person: the tailnet policy is a
    gated kind, so this proposes and somebody else consents.
    """

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = _resource_for_operation(current_key)

    from .tailnet import (
        POLICY_KIND,
        device_at,
        devices,
        may_reach,
        observer,
        policy_allowing,
    )

    known = devices()
    watcher = observer(known)
    if watcher is None:
        raise PolicyError(
            "HQ does not know which tailnet device the controller runs on, so there is nothing to allow access from."
        )

    shut: list[tuple[str, int]] = []
    for item in (resource.status or {}).get("unreachable_consumers") or []:
        if not isinstance(item, dict):
            continue
        target = device_at(str(item.get("endpoint", "")), known)
        try:
            port = int(str(item.get("port", "")) or 0)
        except ValueError:
            continue
        if target is None or not port:
            continue
        verdict = may_reach(watcher.name, target.name, port, known)
        if verdict.known and not verdict.allowed:
            shut.append((target.name, port))
    if not shut:
        raise PolicyError(
            "The tailnet policy does not block any place that could not be reached. Allowing more will not fix this."
        )

    policy = ManagedResource.objects.filter(kind=POLICY_KIND).first()
    if policy is None:
        raise PolicyError("The tailnet policy is not managed in HQ.")
    refuse_while_drifted(policy)

    document = str(policy.spec.get("document", ""))
    moved: list[str] = []
    for target_name, port in sorted(set(shut)):
        amended, summary = policy_allowing(document, source=watcher.name, target=target_name, port=port)
        if amended:
            document, _ = amended, moved.append(summary)
    if not moved:
        raise PolicyError("The tailnet policy already allows every needed path.")

    return save_managed_resource(
        ManagedResourceCommand(
            key=policy.key,
            kind=policy.kind,
            spec={**policy.spec, "document": document},
            enabled=policy.enabled,
        ),
        principal=principal,
        current_key=policy.key,
    )


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
