"""Changes a credential may ask for but only a person may allow.

The incident this exists for took two calls and about a minute. A service token
held on a workstation amended the estate's access policy, then asked for the
amendment to be applied, and the privileged controller applied it to the live
network. Every step was authorized: the token held the infrastructure
capability, the capability registry ran exactly what it was asked to run, and
the controller did precisely its job. Nothing failed. Nobody agreed.

That is the gap this closes. Authority answers "may this caller do it"; it has
never been able to answer "does anybody want it done", and for a credential
sitting on a laptop those are different questions. Anything that comes to hold
the token -- a compromised machine, an automated caller following instructions
it read somewhere, a transcript someone kept -- inherits the authority and
nothing else stands between it and the network.

Four decisions, each of which was the alternative to something worse:

**The unit is the resource kind, not the capability effect.** Effect describes
how forceful an act is. It cannot describe how much stands behind the thing
acted upon, and the two do not correlate: reconciling one DNS rewrite and
pushing a new access policy are the same effect. Gating the effect would put a
decision in front of a person for every container restart, which is how a
decision stops being read. The kind carries the flag, so the estate's most
valuable control is gated and the rest of the estate is untouched.

**It holds the request, not the queued work.** A held request writes nothing:
no declaration, no operation row. There is nothing for the controller to claim,
so no filter has to remember to exclude it, and no later change to how work is
claimed can accidentally let one through. The one place a held request could
become real is the replay below, and the replay only happens from a decision.

**It sits at the capability boundary.** Every non-interactive adapter -- token
API, machine bridge, local command line -- funnels through one function to run a
capability, so one check covers all of them and cannot be bypassed by adding
another adapter. The operator's own surfaces call the use cases directly and are
deliberately unaffected: a click behind an identity provider and a passkey is
the signal this whole module exists to wait for.

**An approval covers a diff, not a resource.** The requested call and the state
it was measured against are fingerprinted together. Approving means approving
that comparison; if the declaration moves in between, the fingerprint no longer
matches and the request is superseded rather than silently applied to content
nobody read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from difflib import unified_diff
import hashlib
import json
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ApprovalRequest, ManagedResource
from control_plane.providers import PROVIDERS
from core.audit import operation_context

from .security import AuthorizationError, Principal, internal_principal, is_interactive

# How long an unanswered request stands. A day, because the person it is waiting
# for sleeps and a request that lapses while they do is a nuisance, whereas one
# that is still clickable a month later is a change nobody is looking at any
# more being applied on the strength of a decision nobody remembers.
DEFAULT_WINDOW_HOURS = 24

# How many outstanding requests one actor may hold. A caller that has already
# asked for ten decisions and been answered none does not need an eleventh: it
# needs to stop. Without a ceiling, the cheapest way past this module is to fill
# the page it is read on.
MAX_PENDING_PER_ACTOR = 10

# Reads never wait for anybody. Stated as the exemption rather than listing the
# effects that are gated, so an effect introduced later is considered rather
# than exempted by omission.
READ_EFFECT = "read"
DECLARATIONS = "infrastructure.resources"

# Rows written while approvals were infrastructure-only keep the old label.
AUDIT_LABEL = "Approval"
AUDIT_LABELS = (AUDIT_LABEL, "Infrastructure approval")


class ApprovalError(ValueError):
    """A decision could not be taken, and this says what a person should know."""


class TooManyPendingApprovals(ApprovalError):
    """One actor is holding more outstanding requests than it may."""


@dataclass(frozen=True)
class ApprovalSubject:
    """What a held request is about, and what it would be measured against."""

    kind: str
    resource_key: str
    baseline: dict[str, Any]


def _window() -> timedelta:
    hours = getattr(settings, "SEVERINO_APPROVAL_WINDOW_HOURS", DEFAULT_WINDOW_HOURS)
    return timedelta(hours=hours)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(
    capability: str, target: Any, payload: dict[str, Any], baseline: dict[str, Any]
) -> str:
    """One digest over the requested call and the state it was read against.

    Both halves matter and for different reasons. The call is what would run, so
    a second request differing by one character is a different decision. The
    baseline is what a person compared it to, so a declaration that moves while
    the request waits makes the comparison they were shown untrue -- and an
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
    """The declaration as it stands, in the three terms a change moves.

    The generation is in here deliberately. Two declarations can carry the same
    spec with different histories, and an approval of "apply what is declared"
    is an approval of the revision that was declared when it was asked for.
    """

    return {
        "spec": resource.spec,
        "enabled": resource.enabled,
        "generation": resource.generation,
        "observed": resource.status or {},
    }


def approval_subject(
    spec, payload: dict[str, Any], target: Any
) -> ApprovalSubject | None:
    """What a held call is about, and its baseline. Shared by hold and approve,
    whose fingerprints must match. None for a read or a missing target.
    """

    if spec.effect == READ_EFFECT:
        return None
    if spec.subject_resource == DECLARATIONS:
        return _declaration_subject(payload, target)
    return _record_subject(spec, target)


def _declaration_subject(payload: dict[str, Any], target: Any) -> ApprovalSubject | None:
    if target is not None:
        resource = ManagedResource.objects.filter(key=str(target)).first()
        if resource is None:
            return None
        return ApprovalSubject(resource.kind, resource.key, _baseline_of(resource))
    # No target means a declaration being brought into existence, and the kind
    # it will have is in the payload.
    return ApprovalSubject(str(payload.get("kind", "")), str(payload.get("key", "")), {})


def _record_subject(spec, target: Any) -> ApprovalSubject | None:
    """A record's baseline is its canonical read, taken internally: it is shown
    to the person deciding, never returned to the caller.
    """

    from .resources import (
        InvalidResourceInput,
        ResourceNotFound,
        UnsupportedResourceOperation,
        get_resource,
    )

    kind = spec.subject_resource or spec.name
    if target is None or not spec.subject_resource:
        return ApprovalSubject(kind, "" if target is None else str(target), {})
    try:
        baseline = get_resource(
            spec.subject_resource,
            target,
            principal=internal_principal("approval-baseline"),
            strict=False,
        )
    except ResourceNotFound:
        return None
    except (UnsupportedResourceOperation, InvalidResourceInput):
        # No canonical read: still holdable, but staleness cannot be detected.
        baseline = {}
    return ApprovalSubject(kind, str(target), baseline)


def may_be_held_by_default(spec) -> bool:
    """The capability-level half of held_by_default, for describing a default without a target."""

    return spec.effect != READ_EFFECT and spec.subject_resource == DECLARATIONS


def held_by_default(spec, payload: dict[str, Any], target: Any) -> bool:
    """The default before any operator rule: a change to a gated declaration waits."""

    if not may_be_held_by_default(spec):
        return False
    if target is not None:
        kind = (
            ManagedResource.objects.filter(key=str(target))
            .values_list("kind", flat=True)
            .first()
        )
        return kind is not None and _gated(kind)
    return _gated(str(payload.get("kind", "")))

def _gated(kind: str) -> bool:
    provider = PROVIDERS.get(kind)
    return bool(provider is not None and provider.requires_approval)


def consent_gap(kind: str, *, principal: Principal) -> str:
    """Why this act has no consent behind it, or "" when it has.

    The same rule as the hold above, asked a second time from inside the use
    cases that actually write. Two enforcement points rather than one, because
    the hold knows only what it can see: it keys on a capability declaring that
    it acts on an infrastructure resource, and a future capability, or an
    extension's, could write one of these declarations without saying so. The
    boundary is the useful check -- it can answer "waiting for approval" instead
    of refusing -- and this is the one that cannot be walked around.

    A string rather than an exception so the caller raises its own domain error,
    which keeps this module free of imports from the use cases that call it.
    """

    if not _gated(kind):
        return ""
    if is_interactive(principal) or principal.approved_by:
        return ""
    return (
        f"Changing a {kind!r} declaration needs a person to approve it. Ask for "
        "the change over an interface that records the request, then approve it "
        "as a signed-in operator."
    )


def hold_for_approval(
    spec, payload: dict[str, Any], target: Any, *, principal: Principal
) -> dict[str, Any] | None:
    """Hold this call for a person, or return nothing if it needs no holding.

    The one call site is the capability runner, immediately after authority and
    payload validation and before the handler. In that order on purpose: a
    caller that may not do this at all is told so, and a malformed request is
    still malformed, rather than either becoming a decision for somebody to read.
    """

    if is_interactive(principal):
        return None
    subject = approval_subject(spec, payload, target)
    if subject is None:
        return None
    digest = fingerprint(spec.name, target, payload, subject.baseline)
    existing = ApprovalRequest.objects.filter(
        capability=spec.name,
        target="" if target is None else str(target),
        content_fingerprint=digest,
        state=ApprovalRequest.State.PENDING,
    ).first()
    if existing is not None:
        # The same ask, again. Answered with the request that is already
        # waiting, so a caller that retries learns nothing new and creates
        # nothing new.
        return _awaiting(existing, repeated=True)
    outstanding = ApprovalRequest.objects.filter(
        requested_actor=principal.actor, state=ApprovalRequest.State.PENDING
    ).count()
    if outstanding >= MAX_PENDING_PER_ACTOR:
        raise TooManyPendingApprovals(
            f"{principal.actor!r} already has {outstanding} changes waiting for "
            "approval. Nothing further is accepted from it until those are "
            "answered."
        )
    reason = str(payload.get("reason", ""))[:300]
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="approval.request",
    ):
        held = ApprovalRequest.objects.create(
            capability=spec.name,
            target="" if target is None else str(target),
            payload=payload,
            resource_kind=subject.kind,
            resource_key=subject.resource_key,
            baseline=subject.baseline,
            content_fingerprint=digest,
            requested_actor=principal.actor,
            requested_interface=principal.interface,
            reason=reason,
            expires_at=timezone.now() + _window(),
        )
    return _awaiting(held, repeated=False)


def _awaiting(held: ApprovalRequest, *, repeated: bool) -> dict[str, Any]:
    """What a non-interactive caller is told, which is not an error.

    It asked for something legitimate and the answer is "a person has to agree
    first". Reported as a success with a state rather than as a failure, because
    a caller told this has nothing to fix and nothing to retry: the one useful
    thing it can do is say so, name the request, and stop. A failure reads as
    something to try again, and something that tries again is how a queue of
    identical decisions gets built.
    """

    return {
        "ok": True,
        "status": "awaiting_approval",
        "queued": False,
        "applied": False,
        "message": (
            "This change is waiting for a person to approve it in HQ. Nothing "
            "has been written and nothing is queued. Tell whoever asked, with "
            "the request id, and stop; approving it is not something a token "
            "can do."
        ),
        "repeated": repeated,
        "approval": serialize_approval(held),
    }


def serialize_approval(held: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": str(held.id),
        "capability": held.capability,
        "target": held.target,
        "resource_kind": held.resource_kind,
        "resource_key": held.resource_key,
        "state": held.state,
        "requested_actor": held.requested_actor,
        "requested_interface": held.requested_interface,
        "reason": held.reason,
        "requested_at": held.created_at.isoformat(),
        "expires_at": held.expires_at.isoformat(),
        "content_fingerprint": held.content_fingerprint,
        "review_url": reverse("core:approval_entry", kwargs={"approval_id": held.id}),
    }


def lapse_unanswered(ids: tuple[Any, ...]) -> int:
    """Mark these requests lapsed, and say how many moved."""

    return ApprovalRequest.objects.filter(
        pk__in=ids, state=ApprovalRequest.State.PENDING
    ).update(
        state=ApprovalRequest.State.EXPIRED,
        decided_at=timezone.now(),
        decision_note="Nobody answered it inside its window.",
    )


def pending(*, limit: int | None = 50) -> tuple[ApprovalRequest, ...]:
    """Every request still waiting for a person, oldest first.

    Expiry happens here, on the way past, rather than on a timer. The only two
    moments that matter are somebody reading the queue and somebody deciding,
    and both go through this or through the check in ``approve`` -- so a lapsed
    request is never approvable whether or not anything has swept it. A timer
    would be a second thing to keep running for a guarantee that already holds.

    One read, and a write only when there is something to write. This is called
    once per dashboard assembly, where the page's whole cost is measured, and an
    unconditional update to mark nothing would be a query spent on every load
    for a queue that is almost always empty.
    """

    now = timezone.now()
    waiting = tuple(
        ApprovalRequest.objects.filter(state=ApprovalRequest.State.PENDING).order_by(
            "created_at"
        )[:limit]
    )
    lapsed = tuple(held.pk for held in waiting if held.expires_at <= now)
    if lapsed:
        lapse_unanswered(lapsed)
    return tuple(held for held in waiting if held.pk not in lapsed)


def _settle(held: ApprovalRequest, state: str, note: str = "") -> None:
    """Record an outcome nobody chose, in its own transaction.

    Its own, because the two callers below go on to raise. Written inside the
    caller's transaction the mark would be rolled back by the very exception it
    exists to explain, and the request would be found still pending -- which
    means the next reader is offered a decision that cannot be taken.
    """

    with transaction.atomic():
        held.state = state
        held.decided_at = timezone.now()
        held.decision_note = note
        held.save(
            update_fields=("state", "decided_at", "decision_note", "updated_at")
        )


def _decidable(approval_id: str) -> ApprovalRequest:
    try:
        held = ApprovalRequest.objects.get(pk=approval_id)
    except (ApprovalRequest.DoesNotExist, ValueError) as exc:
        raise ApprovalError("That approval request was not found.") from exc
    if held.state != ApprovalRequest.State.PENDING:
        raise ApprovalError(
            f"That request is already {held.get_state_display().lower()}."
        )
    if held.expires_at <= timezone.now():
        _settle(
            held,
            ApprovalRequest.State.EXPIRED,
            "Nobody answered it inside its window.",
        )
        raise ApprovalError(
            "That request lapsed before it was answered. Ask for it again if it "
            "is still wanted."
        )
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
        raise AuthorizationError(
            "Approving a held change requires a signed-in operator on the web "
            "interface. A capability is not enough: the point of the hold is "
            "that a person sees it."
        )
    if principal.actor == held.requested_actor:
        raise AuthorizationError("A request cannot be approved by whoever asked for it.")


def approve(approval_id: str, *, principal: Principal) -> dict[str, Any]:
    """Agree to a held change and run exactly the call that was held.

    The replay is the original call, under a principal that still names whoever
    asked for it and now also names who allowed it. Attribution is not moved
    onto the approver: the audit trail has to keep saying that a token asked and
    a person agreed, because those are two different facts and the interesting
    incident is the one where the first happens without the second.

    Deliberately not one transaction from end to end. The checks may have to
    record that a request has lapsed or been superseded and then refuse, and a
    refusal that rolls back its own explanation leaves the request pending for
    the next reader to be offered again. So the marks commit on their own, and
    only the application -- re-reading the row under a lock, because two people
    may be looking at this page -- is atomic.
    """

    from .capabilities import authorize_capability, capability_registry, execute_approved

    held = _decidable(approval_id)
    spec = capability_registry().get(held.capability)
    if spec is None:
        raise ApprovalError(
            f"{held.capability!r} is no longer a capability HQ offers, so this "
            "request cannot be applied."
        )
    _require_person(principal, held)
    # The approver has to be allowed to do the thing themselves. Otherwise this
    # page would be a way to run a capability the clicker does not hold.
    authorize_capability(spec, principal)
    current = approval_subject(spec, held.payload, held.target or None)
    baseline = current.baseline if current is not None else {}
    if fingerprint(held.capability, held.target or None, held.payload, baseline) != (
        held.content_fingerprint
    ):
        _settle(
            held,
            ApprovalRequest.State.STALE,
            "The declaration changed after this was requested.",
        )
        raise ApprovalError(
            "What this request was about has changed since it was asked for, so "
            "approving it would apply something other than what is shown. It "
            "has been superseded; ask again against the current declaration."
        )
    acting = Principal(
        held.requested_actor,
        held.requested_interface,
        principal.capabilities,
        approved_by=principal.actor,
    )
    with transaction.atomic():
        # Re-read under a lock. Two people can have this page open, and an
        # approval applied twice is the change made twice -- which for a queued
        # operation is caught by its idempotency key and for a declaration is not.
        locked = ApprovalRequest.objects.select_for_update().get(pk=held.pk)
        if locked.state != ApprovalRequest.State.PENDING:
            raise ApprovalError(
                f"That request is already {locked.get_state_display().lower()}."
            )
        result = execute_approved(
            spec, locked.payload, locked.target or None, principal=acting
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


def reject(approval_id: str, *, principal: Principal, note: str = "") -> dict[str, Any]:
    """Refuse a held change, so the caller's request has an answer either way."""

    held = _decidable(approval_id)
    _require_person(principal, held)
    with transaction.atomic():
        held.state = ApprovalRequest.State.REJECTED
        held.decided_actor = principal.actor
        held.decided_interface = principal.interface
        held.decided_at = timezone.now()
        held.decision_note = note.strip()[:300]
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


# ----- What a person is shown -------------------------------------------------


@dataclass(frozen=True)
class ChangeRow:
    """One field that would move, and what it would move between."""

    path: str
    before: str
    after: str
    change: str


@dataclass(frozen=True)
class ChangePreview:
    """The comparison a decision is taken on.

    ``rows`` is the semantic reading: a document is parsed and compared by path,
    so an access policy shows the three grants that moved rather than two walls
    of text that differ somewhere. ``lines`` is the honest fallback for content
    that will not parse -- a policy document with comments in it, for instance --
    where a unified diff is the most that can truthfully be said.
    """

    label: str
    rows: tuple[ChangeRow, ...]
    lines: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.rows and not self.lines

    @property
    def compares(self) -> bool:
        """Whether there is a before to show, or only what would be written."""

        return any(row.change != "added" for row in self.rows)


def _expand(value: Any) -> Any:
    """A string that is really a document, read as one.

    The field that carries an access policy is a string as far as the spec is
    concerned, and comparing it as a string is what makes a diff unreadable.
    Parsed, the same change is three paths and their values.
    """

    if isinstance(value, str) and value.strip()[:1] in {"{", "["}:
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    value = _expand(value)
    if isinstance(value, dict):
        found: dict[str, str] = {}
        for key, child in value.items():
            found.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return found
    if isinstance(value, (list, tuple)):
        found = {}
        for index, child in enumerate(value):
            found.update(_flatten(child, f"{prefix}[{index}]"))
        return found
    return {prefix or "value": "" if value is None else str(value)}


def _text_fields(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    """Fields that stayed unparsed text on both sides and differ."""

    return tuple(
        sorted(
            field
            for field in set(before) | set(after)
            if isinstance(_expand(before.get(field, "")), str)
            and isinstance(_expand(after.get(field, "")), str)
            and str(before.get(field, "")) != str(after.get(field, ""))
        )
    )


def compare(before: dict[str, Any], after: dict[str, Any], *, label: str) -> ChangePreview:
    """The paths that differ between two specs, and a line diff where they cannot."""

    text = _text_fields(before, after)
    structural_before = {
        key: value for key, value in before.items() if key not in text
    }
    structural_after = {key: value for key, value in after.items() if key not in text}
    flat_before = _flatten(structural_before)
    flat_after = _flatten(structural_after)
    rows = []
    for path in sorted(set(flat_before) | set(flat_after)):
        was = flat_before.get(path)
        now = flat_after.get(path)
        # Absent and empty are the same nothing. A validated declaration carries
        # every optional field as a blank while the request that would replace it
        # simply omits them, so read literally the diff opens with a row per
        # unset field, each saying a blank became a blank -- and the one line
        # that matters is somewhere underneath.
        if (was or "") == (now or ""):
            continue
        rows.append(
            ChangeRow(
                path=path,
                before="" if was is None else was,
                after="" if now is None else now,
                change=(
                    "added" if was is None else "removed" if now is None else "changed"
                ),
            )
        )
    lines: list[str] = []
    for field in text:
        lines.extend(
            unified_diff(
                str(before.get(field, "")).splitlines(),
                str(after.get(field, "")).splitlines(),
                fromfile=f"{field} (now)",
                tofile=f"{field} (requested)",
                lineterm="",
                n=2,
            )
        )
    return ChangePreview(label=label, rows=tuple(rows), lines=tuple(lines))


def preview(held: ApprovalRequest) -> ChangePreview:
    """What approving this request would change, as a person needs to read it.

    Two different comparisons, because two different things are being asked.
    Amending a declaration is a change to what HQ intends, so it is read against
    what HQ intends now. Applying one is a change to the world, so it is read
    against what the world was last seen holding -- which is the only reading
    that answers "what happens to the network if I click this".
    """

    from .capabilities import capability_registry

    spec = capability_registry().get(held.capability)
    if spec is not None and spec.subject_resource != DECLARATIONS:
        return _record_preview(spec, held)
    declared = dict(held.baseline.get("spec") or {})
    provider = PROVIDERS.get(held.resource_kind)
    requested = held.payload.get("spec")
    if isinstance(requested, dict):
        return compare(declared, requested, label="Declaration would change")
    if held.capability.endswith(".remove"):
        return compare(declared, {}, label="This declaration would be removed")
    observed = held.baseline.get("observed") or {}
    if provider is not None and provider.from_record is not None and observed:
        try:
            live = provider.from_record(observed)
        except (AttributeError, KeyError, TypeError, ValueError):
            live = {}
        if live:
            return compare(live, declared, label="The live record would change")
    return compare({}, declared, label="This would be applied as declared")


# Fields that steer a request rather than being written by it.
_CONTROL_FIELDS = frozenset({"confirm", "idempotency_key", "reason"})


def _record_preview(spec, held: ApprovalRequest) -> ChangePreview:
    """A record's fields, as they are and as they would be. No document diffing:
    a project's description is a value, not a policy to parse.
    """

    fields = {
        key: value
        for key, value in held.payload.items()
        if key not in _CONTROL_FIELDS and value not in ("", None, [], {})
    }
    if spec.effect == "destructive":
        return _field_rows(held.baseline, {}, label="Would be deleted")
    if not held.target:
        return _field_rows({}, fields, label="Would be created")
    if fields:
        return _field_rows(
            {key: held.baseline.get(key) for key in fields}, fields, label="Would change"
        )
    return ChangePreview(label=f"Would run on {held.target}", rows=(), lines=())


def _field_rows(before: dict[str, Any], after: dict[str, Any], *, label: str) -> ChangePreview:
    rows = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if (was or "") == (now or ""):
            continue
        rows.append(
            ChangeRow(
                path=key,
                before="" if was is None else str(was),
                after="" if now is None else str(now),
                change="added" if was is None else "removed" if now is None else "changed",
            )
        )
    return ChangePreview(label=label, rows=tuple(rows), lines=())


# Held requests are decided on their audit entry. State stays on ApprovalRequest.


def review(held: ApprovalRequest) -> dict[str, Any]:
    """What the decision card shows."""

    from .capabilities import capability_label

    return {
        "held": held,
        "title": capability_label(held.capability),
        "preview": preview(held),
        "resource_url": (
            reverse("control_plane:detail", kwargs={"key": held.resource_key})
            if held.resource_kind
            and ManagedResource.objects.filter(key=held.resource_key).exists()
            else ""
        ),
    }


def for_audit_event(event) -> ApprovalRequest | None:
    """The request an audit row is about, if it is about one."""

    if event.object_type not in AUDIT_LABELS or not event.object_id:
        return None
    try:
        return ApprovalRequest.objects.filter(pk=event.object_id).first()
    except (ValueError, DjangoValidationError):
        return None


def entry_event(approval_id) -> Any:
    """The audit row that records this request being made."""

    from core.models import AuditLog

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

    return tuple(str(held.pk) for held in pending(limit=None))
