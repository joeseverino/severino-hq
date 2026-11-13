"""Changes a credential may ask for but only a person may allow.

Authority answers "may this caller do it", never "does anybody want it done",
and anything that comes to hold a workstation's token (a compromised machine,
an agent following instructions it read somewhere) inherits its authority. So
for the kinds that matter most a person has to agree; what a held request may
and may not do is docs/Security.md#what-hq-enforces.
"""

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from typing import Annotated, Any, Final

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource
from hq.platform.core.audit import audit_operation
lazy from hq.domains.control_plane.providers import record_spec
lazy from hq.platform.core.models import AuditLog

from . import derivations
from .command_fields import Describe
from .derivations import holds_until
from .held_answer import ApprovalApplied, ApprovalRejected, HeldCall, serialize_approval
from .record_errors import PolicyError, RecordNotFound
from .refusal_status import refusal_of, refuse
from .security import (
    INTERNAL_INTERFACE,
    OPERATOR_INTERFACES,
    AuthorizationError,
    Capability,
    Principal,
    internal_principal,
    is_interactive,
)
lazy from . import agent_access, capabilities, resources
lazy from .approval_review import asked
lazy from .capabilities import authorize_capability, execute_approved, validate_command
lazy from .command_targets import item_name
lazy from .integration_specs import capability_fields
lazy from .resource_refusals import UnsupportedResourceOperation
lazy from .resources import InvalidResourceInput
lazy from .ui import counted

# How many outstanding requests one actor may hold. A caller that has already
# asked for ten decisions and been answered none does not need an eleventh: it
# needs to stop. Without a ceiling, the cheapest way past this module is to fill
# the page it is read on.
MAX_PENDING_PER_ACTOR = 10

# Reads never wait for anybody. Stated as the exemption rather than listing the
# effects that are gated, so an effect introduced later is considered rather
# than exempted by omission.
READ_EFFECT = "read"
# What takes something away from whoever is using it: a deletion, or a stop.
DESTRUCTIVE_EFFECT = "destructive"
# Payload fields that turn a write into deleting whatever it does not name: an
# orphan prune removes every record the manifest leaves out, restricted ones
# included. Such a call is held like a destructive capability, whatever the
# capability's own effect says.
DELETING_FLAGS = ("prune_orphans",)


def is_destructive(spec, command: Any) -> bool:
    """Whether this call takes something away: by its effect, or by what it asks for."""

    if spec.effect == DESTRUCTIVE_EFFECT:
        return True
    return any(getattr(command, flag, False) is True for flag in DELETING_FLAGS)


DECLARATIONS = "infrastructure"

# Existing audit rows may carry either label; both are read as approvals.
AUDIT_LABEL = "Approval"
AUDIT_LABELS = (AUDIT_LABEL, "Infrastructure approval")


class ApprovalError(PolicyError):
    """A decision could not be taken, and this says what a person should know."""


class Settled(ApprovalError):
    """A refusal that first marked the request lapsed or stale (``_settle``).

    A posted decision runs inside its retry key's transaction, which a raise
    rolls back, mark and audit row with it; ``answered`` returns it instead.
    """


class RefusedHold(ApprovalError):
    """A call that had to wait for a person and could not be made to.

    `reason` is written for the caller, so an adapter returns it rather than
    the exception's string form, under `code`.
    """

    code = "refused_hold"


class TooManyPendingApprovals(RefusedHold):
    """One actor is holding more outstanding requests than it may."""

    code = "too_many_pending_approvals"


class UnreadableSubject(RefusedHold):
    """A call a person must approve is about something HQ cannot show them.

    Refused rather than run: with no subject there is nothing to approve and no
    baseline to hold the approval to, and running it would turn "must wait for a
    person" into "runs whenever the read comes back empty".
    """

    code = "approval_subject_unreadable"


@dataclass(frozen=True, slots=True)
class ApprovalSubject:
    """What a held request is about, and what it would be measured against."""

    kind: str
    resource_key: str
    baseline: dict[str, Any]


def _window() -> timedelta:
    hours = settings.SEVERINO_APPROVAL_WINDOW_HOURS
    return timedelta(hours=hours)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(capability: str, target: Any, payload: dict[str, Any], baseline: dict[str, Any]) -> str:
    """One digest over the requested call and the state it was read against.

    Both halves matter and for different reasons. The call is what would run, so
    a second request differing by one character is a different decision. The
    baseline is what a person compared it to, so a declaration that moves while
    the request waits makes the comparison they were shown untrue, and an
    approval of an untrue comparison is not an approval of anything.
    """

    material = _canonical(
        {
            "capability": capability,
            "target": "" if target is None else str(target),
            "payload": payload,
            "baseline": baseline,
        }
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _baseline_of(resource: ManagedResource) -> dict[str, Any]:
    """The declaration as it stands, in the three terms a change moves, and the
    live record as the spec it reproduces, which an apply is compared against.

    The generation is in here deliberately. Two declarations can carry the same
    spec with different histories, and an approval of "apply what is declared"
    is an approval of the revision that was declared when it was asked for. The
    live record is its spec alone: every sweep rewrites the rest of what it read.
    """

    return {
        "spec": resource.spec,
        "enabled": resource.enabled,
        "generation": resource.generation,
        "observed": (resource.status and record_spec(resource.kind, resource.status)) or {},
    }


# Where ``measured_by`` puts a handler's own reading of what it acts on.
_MEASURED = "measured"


def measured_by[Handler: Callable[..., Any]](
    subject: Callable[[Any], ApprovalSubject],
) -> Callable[[Handler], Handler]:
    """Declare what a held call of a handler is about, read from its validated
    command, for a command that decides more than its target and payload say:
    a run's hosts resolve through HQ's facts, which move while it waits."""

    def declare(handler: Handler) -> Handler:
        setattr(handler, _MEASURED, subject)
        return handler

    return declare


# Where ``waits_for_a_person`` puts a handler's test of a call too wide for a standing rule.
_WAITS = "waits"
# The key of a measured baseline that holds the comparison a person decides
# on, as ``{"before": ..., "after": ...}`` (``approval_review.preview``).
COMPARED = "compared"
# The key of a measured baseline that holds what the change reaches that a
# person should weigh before approving it, each a sentence the card warns with
# (``approval_review.review``).
WARNINGS = "warnings"


def waits_for_a_person[Handler: Callable[..., Any]](
    always: Callable[[Any], bool],
) -> Callable[[Handler], Handler]:
    """Declare that an agent's call of a handler waits for a person unless
    its own rule allows it, and that a call ``always`` holds true of waits
    whatever rule it has: a change wider than any standing consent covers."""

    def declare(handler: Handler) -> Handler:
        setattr(handler, _WAITS, always)
        return handler

    return declare


def every_call(_command: Any) -> bool:
    """``waits_for_a_person``'s test for a handler whose every call from an agent waits."""

    return True


def no_rule_allows(spec) -> bool:
    """Whether every call of ``spec`` an agent asks waits for a person, so no agent rule may allow it."""

    return waits(spec) is every_call


def waits(spec) -> Callable[[Any], bool] | None:
    """The handler's test of a call no rule may let past a person, when it waits for one by default."""

    # Read from the handler's own attributes, so a stand-in that answers every
    # name (a mock) declares nothing.
    return vars(spec.handler).get(_WAITS) if hasattr(spec.handler, "__dict__") else None


def approval_subject(spec, command: Any, target: Any) -> ApprovalSubject | None:
    """What a held call is about, and its baseline. Shared by hold and approve,
    whose fingerprints must match. None for a read or a missing target.
    """

    if spec.effect == READ_EFFECT:
        return None
    if (measured := getattr(spec.handler, _MEASURED, None)) is not None:
        return measured(command)
    if spec.subject_resource == DECLARATIONS:
        return _declaration_subject(command, target)
    return _record_subject(spec, target)


def _declaration_subject(command: Any, target: Any) -> ApprovalSubject | None:
    if target is not None:
        resource = ManagedResource.objects.filter(key=str(target)).first()
        if resource is None:
            return None
        return ApprovalSubject(resource.kind, resource.key, _baseline_of(resource))
    # No target means a declaration being brought into existence, and the kind
    # it will have is in the command.
    return ApprovalSubject(str(getattr(command, "kind", "")), str(getattr(command, "key", "")), {})


def _record_subject(spec, target: Any) -> ApprovalSubject | None:
    """A record's baseline is its canonical read, taken internally: it is shown
    to the person deciding, never returned to the caller.
    """

    kind = spec.subject_resource or spec.name
    if target is None or not spec.subject_resource:
        return ApprovalSubject(kind, "" if target is None else str(target), {})
    try:
        baseline = resources.get_resource(
            spec.subject_resource,
            target,
            principal=internal_principal("approval-baseline"),
            strict=False,
        )
    except RecordNotFound:
        return None
    except UnsupportedResourceOperation, InvalidResourceInput:
        # No canonical read: still holdable, but staleness cannot be detected.
        baseline = {}
    if baseline and spec.effect != DESTRUCTIVE_EFFECT:
        # A change shows the fields it sends, as they are now, and the record's
        # name; the rest of a read (a last push, open pulls) moves with sweeps.
        read = baseline.get("resource", baseline)
        sent = {key: baseline[key] for key in capability_fields(spec) if key in baseline}
        baseline = {"name": item_name(read) if isinstance(read, dict) else "", **sent}
    return ApprovalSubject(kind, str(target), baseline)


def hold_for_approval(
    spec, payload: dict[str, Any], command: Any, target: Any, *, principal: Principal
) -> HeldCall | None:
    """Hold this call for a person, or return nothing if it needs no holding.

    The one call site is the capability runner, immediately after authority and
    payload validation and before the handler. In that order on purpose: a
    caller that may not do this at all is told so, and a malformed request is
    still malformed, rather than either becoming a decision for somebody to read.
    What it is about is read from ``command``; ``payload`` is kept to replay.
    """

    # Every non-interactive adapter runs a capability through the one runner,
    # so this check covers each one and a new adapter cannot bypass it. The
    # operator's own signed-in surfaces are the consent this waits for.
    if is_interactive(principal):
        return None
    subject = approval_subject(spec, command, target)
    if subject is None:
        raise UnreadableSubject(
            "This change needs approval, but HQ could not read what it would change. Check the target and ask again."
        )
    digest = fingerprint(spec.name, target, payload, subject.baseline)
    existing = _asked_already(spec.name, target, digest, principal)
    if existing is not None:
        # The same ask, again, from the same caller. Answered with the request
        # that is already waiting, so a caller that retries learns nothing new
        # and creates nothing new.
        return _awaiting(existing, repeated=True)
    outstanding = ApprovalRequest.objects.filter(
        requested_actor=principal.actor,
        requested_person_key=principal.person_key,
        state=ApprovalRequest.State.PENDING,
    ).count()
    if outstanding >= MAX_PENDING_PER_ACTOR:
        raise TooManyPendingApprovals(
            f"This caller already has {counted(outstanding, 'change')} waiting for "
            "approval. HQ takes no more from it until those are decided."
        )
    reason = str(getattr(command, "reason", ""))[:300]
    # The request alone: no declaration and no operation row, so there is
    # nothing for the controller to claim and no filter has to exclude it.
    held = ApprovalRequest(
        capability=spec.name,
        target="" if target is None else str(target),
        payload=payload,
        resource_kind=subject.kind,
        resource_key=subject.resource_key,
        baseline=subject.baseline,
        content_fingerprint=digest,
        requested_actor=principal.actor,
        requested_interface=principal.interface,
        requested_device=principal.device,
        requested_person_key=principal.person_key,
        reason=reason,
        expires_at=timezone.now() + _window(),
    )
    with _its_operation(held, REQUEST, principal):
        # The audit says it as the card does.
        held.audit_said = str(asked(held))
        try:
            with transaction.atomic():
                held.save(force_insert=True)
        except IntegrityError:
            # The same caller's identical ask, written between the read above
            # and this one: answered as the repeat it is.
            existing = _asked_already(spec.name, target, digest, principal)
            if existing is None:
                raise
            return _awaiting(existing, repeated=True)
    return _awaiting(held, repeated=False)


def _asked_already(capability: str, target: Any, digest: str, principal: Principal) -> ApprovalRequest | None:
    """The pending request this caller already made for this exact change, if any.

    Keyed on the asker as well as the change: a hold is the asker's own, so a
    second caller asking the same thing gets its own request, read and
    approved as itself, never a way into the first caller's.
    """

    return ApprovalRequest.objects.filter(
        capability=capability,
        target="" if target is None else str(target),
        content_fingerprint=digest,
        requested_actor=principal.actor,
        requested_interface=principal.interface,
        requested_person_key=principal.person_key,
        state=ApprovalRequest.State.PENDING,
    ).first()


AWAITING_APPROVAL: Final = "awaiting_approval"


def is_held(result: dict[str, Any]) -> bool:
    """Whether a capability's answer is a call waiting for a person."""

    return result.get("status") == AWAITING_APPROVAL


def _awaiting(held: ApprovalRequest, *, repeated: bool) -> HeldCall:
    """What a non-interactive caller is told, which is not an error.

    A success with a state, because the caller has nothing to fix and nothing
    to retry: a failure reads as something to try again, and a caller that
    tries again builds a queue of identical decisions.
    """

    return {
        "ok": True,
        "status": AWAITING_APPROVAL,
        "queued": False,
        "applied": False,
        "message": (
            "This change waits for approval in HQ. Nothing is written or queued "
            "until an operator approves it. Report the request id and stop, "
            "since a token cannot approve it."
        ),
        "repeated": repeated,
        "approval": serialize_approval(held),
    }


def asked_by(approval_id: str, *, principal: Principal) -> ApprovalRequest | None:
    """The held request ``principal`` itself asked for, by its id, or None.

    None as much for another caller's request as for an id nothing holds, so
    a caller learns nothing of a request that is not its own.
    """

    try:
        return ApprovalRequest.objects.filter(
            pk=approval_id,
            requested_actor=principal.actor,
            requested_interface=principal.interface,
            requested_person_key=principal.person_key,
        ).first()
    except ValueError, DjangoValidationError:
        return None


# How a request that ran nothing ended, as the refusal its caller is answered with.
_UNAPPLIED = {
    ApprovalRequest.State.EXPIRED: (
        "expired",
        "This request expired before it was decided, so nothing was written. Ask again if you still need the change.",
    ),
    ApprovalRequest.State.STALE: (
        "stale",
        (
            "What this change acts on changed after you asked, so nothing was written. "
            "Ask again if you still need the change."
        ),
    ),
}


def outcome(held: ApprovalRequest) -> dict[str, Any] | None:
    """What the held call answers once it is settled, or None while a person may still decide it.

    Approved, it is what the call answered when it ran. Any other end is a
    refusal in the shape a capability refuses in, so the caller reads it as
    it would the call refused outright: declined, with the person's note, or
    lapsed or overtaken with nothing written.
    """

    state = held.state
    if state == ApprovalRequest.State.PENDING and held.expires_at <= derivations.clock():
        state = ApprovalRequest.State.EXPIRED
    if state == ApprovalRequest.State.PENDING:
        return None
    if state == ApprovalRequest.State.APPROVED:
        return held.result
    if state == ApprovalRequest.State.REJECTED:
        note = held.decision_note.strip()
        said = f"An operator declined this change in HQ: {note}" if note else "An operator declined this change in HQ."
        return refuse("declined", said)
    code, said = _UNAPPLIED[state]
    return refuse(code, said)


def lapse_expired() -> int:
    """Mark every request past its expiry lapsed, and say how many moved.

    For the hourly sweep: an expired request is already out of ``pending`` and
    refused by ``approve``, so this makes the record say what is already true.
    """

    principal = internal_principal("approvals")
    lapsed = 0
    for approval_id in ApprovalRequest.objects.filter(
        state=ApprovalRequest.State.PENDING, expires_at__lte=timezone.now()
    ).values_list("pk", flat=True):
        # One at a time, so each lapse is audited as part of its request.
        with transaction.atomic():
            held = (
                ApprovalRequest.objects.select_for_update()
                .filter(pk=approval_id, state=ApprovalRequest.State.PENDING, expires_at__lte=timezone.now())
                .first()
            )
            if held is not None:
                # HQ's own lapse of the request, whoever runs the sweep.
                _settle(
                    held,
                    ApprovalRequest.State.EXPIRED,
                    _LAPSED_NOTE,
                    operation=LAPSE,
                    principal=principal,
                    rebinds=True,
                )
                lapsed += 1
    return lapsed


def reads_every_hold(principal: Principal) -> bool:
    """Whether ``principal`` reads every held request, rather than only its own.

    A person decides them, and HQ composes the person's queue, so both read
    them all. Any other caller is a credential, which reads only what it asked
    for: another caller's requests, and who made them, are not its business.
    An allowlist, so an interface added later reads only its own.
    """

    return principal.interface in OPERATOR_INTERFACES or principal.interface == INTERNAL_INTERFACE


def pending(*, principal: Principal, limit: int | None = 50) -> tuple[ApprovalRequest, ...]:
    """Every request still waiting for a person that ``principal`` may read, oldest first.

    A read: one past its expiry is left out whether or not the sweep has
    marked it lapsed (``lapse_expired``), so it is never offered for a
    decision ``approve`` would refuse.
    """

    principal.require(Capability.READ)
    waiting = ApprovalRequest.objects.filter(state=ApprovalRequest.State.PENDING, expires_at__gt=derivations.clock())
    if not reads_every_hold(principal):
        waiting = waiting.filter(
            requested_actor=principal.actor,
            requested_interface=principal.interface,
            requested_person_key=principal.person_key,
        )
    waiting = tuple(waiting.order_by("created_at")[:limit])
    # The answer changes as the first of them expires.
    for held in waiting:
        holds_until(held.expires_at)
    return waiting


def _its_operation(
    held: ApprovalRequest, operation: str, principal: Principal, *, rebinds: bool = False
) -> AbstractContextManager[None]:
    """Bind the operation every row of ``held``'s life is part of: its request,
    its decision, the change it applies and its lapse share its id."""

    return audit_operation(operation=operation, principal=principal, operation_id=str(held.id), rebinds=rebinds)


def _settle(
    held: ApprovalRequest, state: str, note: str, *, operation: str, principal: Principal, rebinds: bool = False
) -> None:
    """Record an outcome nobody chose, in a savepoint of its own.

    The two callers below go on to raise ``Settled``, which must not leave the
    caller's transaction raised: rolled back, the mark the refusal explains is
    gone and the request is found still pending, offering the next reader a
    decision that cannot be taken.
    """

    with _its_operation(held, operation, principal, rebinds=rebinds), transaction.atomic():
        held.state = state
        held.decided_at = timezone.now()
        held.decision_note = note
        held.save(update_fields=("state", "decided_at", "decision_note", "updated_at"))


def _decidable(approval_id: str, principal: Principal) -> ApprovalRequest:
    try:
        held = ApprovalRequest.objects.get(pk=approval_id)
    except (ApprovalRequest.DoesNotExist, ValueError) as exc:
        raise ApprovalError("That approval request was not found.") from exc
    if held.state != ApprovalRequest.State.PENDING:
        raise ApprovalError(f"That request is already {held.get_state_display().lower()}.")
    if held.expires_at <= timezone.now():
        _settle(held, ApprovalRequest.State.EXPIRED, _LAPSED_NOTE, operation=LAPSE, principal=principal)
        raise Settled("That request expired before it was decided, so nothing was written.")
    return held


def _require_person(principal: Principal, held: ApprovalRequest) -> None:
    """Only a person, and not the caller that asked.

    The interface is the test rather than a capability, and that is the whole
    point of the module. The requesting token already held every capability its
    request needed; a capability to approve would be one more thing that token
    could be granted, or could be found to have been granted, and the gate would
    be back where it started.
    """

    if not is_interactive(principal):
        raise AuthorizationError("Approve this in the browser, signed in. An agent cannot approve a change.")
    if principal.actor == held.requested_actor:
        raise AuthorizationError("You cannot approve a request you asked for.")


def _require_asker_running(held: ApprovalRequest) -> None:
    """Pausing agents or blocking one stops what it already asked for, not only what it asks next."""

    if ApprovalRequest.objects.filter(agent_access.held_askers(), pk=held.pk).exists():
        raise ApprovalError(
            "The agent that asked for this is paused or blocked. Resume or allow it to approve this, or decline it."
        )


REQUEST = "approval.request"
APPROVE = "approval.approve"
REJECT = "approval.reject"
LAPSE = "approval.lapse"
_LAPSED_NOTE = "It expired without an answer."


@dataclass(frozen=True, slots=True)
class ApproveCommand:
    """The held request to approve."""

    approval_id: Annotated[str, Describe("The held request to approve.")]


@dataclass(frozen=True, slots=True)
class RejectCommand:
    """The held request to decline, and a note for the agent that asked."""

    approval_id: Annotated[str, Describe("The held request to decline.")]
    note: Annotated[str, Describe("A note for the agent that asked.")] = ""


def approve(command: ApproveCommand, *, principal: Principal) -> ApprovalApplied:
    """Agree to a held change and run exactly the call that was held.

    The replay runs under a principal that names whoever asked and who allowed
    it, so the audit trail keeps saying that a token asked and a person agreed.
    Not one transaction end to end: a lapsed or superseded mark is written
    before the refusal (``Settled``), and only the application is atomic.
    """

    held = _decidable(command.approval_id, principal)
    with _its_operation(held, APPROVE, principal):
        return _apply(held, principal)


def _apply(held: ApprovalRequest, principal: Principal) -> ApprovalApplied:
    spec = capabilities.capability_registry().get(held.capability)
    if spec is None:
        raise ApprovalError("What this request asks for is no longer something HQ can do, so it cannot be applied.")
    _require_person(principal, held)
    _require_asker_running(held)
    # The approver has to be allowed to do the thing themselves. Otherwise this
    # page would be a way to run a capability the clicker does not hold.
    authorize_capability(spec, principal)
    command = validate_command(spec, held.payload, held.target or None)
    current = approval_subject(spec, command, held.target or None)
    baseline = current.baseline if current is not None else {}
    if fingerprint(held.capability, held.target or None, held.payload, baseline) != (held.content_fingerprint):
        _settle(
            held,
            ApprovalRequest.State.STALE,
            "What it would change moved after it was requested.",
            operation=APPROVE,
            principal=principal,
        )
        raise Settled(
            "This changed after the agent asked, so approving it would apply "
            "something other than what is shown. Ask the agent to request it again."
        )
    acting = Principal(
        held.requested_actor,
        held.requested_interface,
        principal.capabilities,
        approved_by=principal.actor,
        person_key=held.requested_person_key,
        device=held.requested_device,
    )
    with transaction.atomic():
        # Re-read under a lock. Two people can have this page open, and an
        # approval applied twice is the change made twice, which for a queued
        # operation is caught by its idempotency key and for a declaration is not.
        locked = ApprovalRequest.objects.select_for_update().get(pk=held.pk)
        if locked.state != ApprovalRequest.State.PENDING:
            raise ApprovalError(f"That request is already {locked.get_state_display().lower()}.")
        # The one binding that runs as someone other than whoever started the
        # operation: the approver agreed, and the change is the requester's.
        with audit_operation(operation=spec.name, principal=acting, operation_id=str(held.id), rebinds=True):
            result = execute_approved(
                spec, command, locked.target or None, principal=acting, sent=frozenset(held.payload)
            )
        locked.state = ApprovalRequest.State.APPROVED
        locked.decided_actor = principal.actor
        locked.decided_interface = principal.interface
        locked.decided_at = timezone.now()
        locked.result = result
        locked.save(
            update_fields=(
                "state",
                "decided_actor",
                "decided_interface",
                "decided_at",
                "result",
                "updated_at",
            )
        )
    return {"ok": True, "approved": str(locked.id), "result": result}


def answered[**P, R](decide: Callable[P, R]) -> Callable[P, R | dict[str, Any]]:
    """``decide`` as its capability runs: a ``Settled`` refusal is answered, so
    the transaction it ran in commits the mark."""

    @wraps(decide)
    def run(*args: P.args, **kwargs: P.kwargs) -> R | dict[str, Any]:
        try:
            return decide(*args, **kwargs)
        except Settled as settled:
            return refusal_of(settled).answer()

    return run


def reject(command: RejectCommand, *, principal: Principal) -> ApprovalRejected:
    """Refuse a held change, so the caller's request has an answer either way."""

    held = _decidable(command.approval_id, principal)
    _require_person(principal, held)
    with _its_operation(held, REJECT, principal), transaction.atomic():
        held.state = ApprovalRequest.State.REJECTED
        held.decided_actor = principal.actor
        held.decided_interface = principal.interface
        held.decided_at = timezone.now()
        held.decision_note = command.note.strip()[:300]
        held.save(
            update_fields=(
                "state",
                "decided_actor",
                "decided_interface",
                "decided_at",
                "decision_note",
                "updated_at",
            )
        )
    return {"ok": True, "rejected": str(held.id)}


def for_audit_event(event) -> ApprovalRequest | None:
    """The request an audit row is about, if it is about one."""

    if event.object_type not in AUDIT_LABELS or not event.object_id:
        return None
    try:
        return ApprovalRequest.objects.filter(pk=event.object_id).first()
    except ValueError, DjangoValidationError:
        return None


def entry_event(approval_id) -> Any:
    """The audit row that records this request being made."""

    return (
        AuditLog.objects.filter(
            object_type__in=AUDIT_LABELS,
            object_id=str(approval_id),
            action=AuditLog.Action.CREATED,
        )
        .order_by("id")
        .first()
    )


def awaiting_ids() -> tuple[str, ...]:
    """pending(), as audit object ids."""

    return tuple(str(held.pk) for held in pending(principal=internal_principal("approvals"), limit=None))
