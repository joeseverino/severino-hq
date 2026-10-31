"""Changes a credential may ask for but only a person may allow.

Authority answers "may this caller do it", never "does anybody want it done",
and anything that comes to hold a workstation's token (a compromised machine,
an agent following instructions it read somewhere) inherits its authority. So
for the kinds that matter most a person has to agree; what a held request may
and may not do is docs/Security.md#what-hq-enforces.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from difflib import unified_diff
from typing import Annotated, Any

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource, OperationRequest
from hq.platform.application.routes import reverse
from hq.platform.core.audit import operation_context
lazy from hq.domains.control_plane.providers import PROVIDERS
lazy from hq.platform.core.models import AuditLog

from . import derivations
from .command_fields import Describe
from .derivations import holds_until
from .payload_words import ChangeRow, changed_words, document, field_words, value_words
from .record_errors import PolicyError, RecordNotFound, Sentence
from .security import AuthorizationError, Principal, internal_principal, is_interactive
lazy from . import capabilities, resources
lazy from .agent_access import agents_paused
lazy from .capabilities import authorize_capability, execute_approved, validate_command
lazy from .command_forms import switch_field
lazy from .command_targets import item_name
lazy from .entity_links import entity_link, record_name
lazy from .integration_specs import TARGET, capability_schema
lazy from .labels import human_label, lower_first
lazy from .resource_capabilities import LIFECYCLE_VERBS
lazy from .resource_operations import ACTION_CAPABILITY
lazy from .resources import InvalidResourceInput, UnsupportedResourceOperation
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


DECLARATIONS = "infrastructure.resources"

# Existing audit rows may carry either label; both are read as approvals.
AUDIT_LABEL = "Approval"
AUDIT_LABELS = (AUDIT_LABEL, "Infrastructure approval")


class ApprovalError(PolicyError):
    """A decision could not be taken, and this says what a person should know."""


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


def approval_subject(spec, command: Any, target: Any) -> ApprovalSubject | None:
    """What a held call is about, and its baseline. Shared by hold and approve,
    whose fingerprints must match. None for a read or a missing target.
    """

    if spec.effect == READ_EFFECT:
        return None
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
    return ApprovalSubject(kind, str(target), baseline)


def may_be_held_by_default(spec) -> bool:
    """The capability-level half of held_by_default, for describing a default without a target."""

    return spec.effect != READ_EFFECT and spec.subject_resource == DECLARATIONS


def held_by_default(spec, command: Any, target: Any) -> bool:
    """The default before any operator rule: a change to a gated declaration waits."""

    if not may_be_held_by_default(spec):
        return False
    if target is not None:
        kind = ManagedResource.objects.filter(key=str(target)).values_list("kind", flat=True).first()
        return kind is not None and _gated(kind)
    return _gated(str(getattr(command, "kind", "")))


def _gated(kind: str) -> bool:
    # The unit is the resource kind, never the capability effect: an effect says
    # how forceful an act is, not how much stands behind what it acts on, and
    # gating it would put every container restart in front of a person.
    provider = PROVIDERS.get(kind)
    return bool(provider is not None and provider.requires_approval)


def consent_gap(kind: str, *, principal: Principal) -> Sentence:
    """Why this act has no consent behind it, or "" when it has.

    The hold's rule, asked again from inside the use cases that write, since
    the hold sees only capabilities that declare they act on an infrastructure
    resource and one that writes a declaration without saying so would pass it.
    A string, so the caller raises its own domain error and this module imports
    none of the use cases that call it.
    """

    if not _gated(kind):
        return ""
    if is_interactive(principal) or principal.approved_by:
        return ""
    return (
        "This change needs a person to approve it. Ask for it through the API or "
        "MCP, which records the request, then approve it in the browser, signed in."
    )


def hold_for_approval(
    spec, payload: dict[str, Any], command: Any, target: Any, *, principal: Principal
) -> dict[str, Any] | None:
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
            "This needs a person to approve it, but HQ could not read what it "
            "would change. Check the target and ask again."
        )
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
            f"This caller already has {counted(outstanding, 'change')} waiting for "
            "approval. Nothing further is accepted from it until those are "
            "answered."
        )
    reason = str(getattr(command, "reason", ""))[:300]
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="approval.request",
    ):
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
            reason=reason,
            expires_at=timezone.now() + _window(),
        )
        # The audit says it as the card does.
        held.audit_said = str(asked(held))
        held.save(force_insert=True)
    return _awaiting(held, repeated=False)


AWAITING_APPROVAL = "awaiting_approval"


def is_held(result: dict[str, Any]) -> bool:
    """Whether a capability's answer is a call waiting for a person."""

    return result.get("status") == AWAITING_APPROVAL


def _awaiting(held: ApprovalRequest, *, repeated: bool) -> dict[str, Any]:
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


def lapse_expired() -> int:
    """Mark every request past its expiry lapsed, and say how many moved.

    For the hourly sweep: an expired request is already out of ``pending`` and
    refused by ``approve``, so this makes the record say what is already true.
    """

    return ApprovalRequest.objects.filter(state=ApprovalRequest.State.PENDING, expires_at__lte=timezone.now()).update(
        state=ApprovalRequest.State.EXPIRED,
        decided_at=timezone.now(),
        decision_note="It expired without an answer.",
    )


def pending(*, limit: int | None = 50) -> tuple[ApprovalRequest, ...]:
    """Every request still waiting for a person, oldest first.

    A read: one past its expiry is left out whether or not the sweep has
    marked it lapsed (``lapse_expired``), so it is never offered for a
    decision ``approve`` would refuse.
    """

    waiting = tuple(
        ApprovalRequest.objects.filter(
            state=ApprovalRequest.State.PENDING, expires_at__gt=derivations.clock()
        ).order_by("created_at")[:limit]
    )
    # The answer changes as the first of them expires.
    for held in waiting:
        holds_until(held.expires_at)
    return waiting


def _settle(held: ApprovalRequest, state: str, note: str = "") -> None:
    """Record an outcome nobody chose, in its own transaction.

    Its own, because the two callers below go on to raise. Written inside the
    caller's transaction the mark would be rolled back by the very exception it
    exists to explain, and the request would be found still pending, which
    means the next reader is offered a decision that cannot be taken.
    """

    with transaction.atomic():
        held.state = state
        held.decided_at = timezone.now()
        held.decision_note = note
        held.save(update_fields=("state", "decided_at", "decision_note", "updated_at"))


def _decidable(approval_id: str) -> ApprovalRequest:
    try:
        held = ApprovalRequest.objects.get(pk=approval_id)
    except (ApprovalRequest.DoesNotExist, ValueError) as exc:
        raise ApprovalError("That approval request was not found.") from exc
    if held.state != ApprovalRequest.State.PENDING:
        raise ApprovalError(f"That request is already {held.get_state_display().lower()}.")
    if held.expires_at <= timezone.now():
        _settle(
            held,
            ApprovalRequest.State.EXPIRED,
            "It expired without an answer.",
        )
        raise ApprovalError("That request expired before it was answered. Ask for it again if it is still wanted.")
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
        raise AuthorizationError("A request cannot be approved by whoever asked for it.")


# The surfaces an agent reaches HQ through; what one of them asked for is an
# agent's request, and pausing agents pauses it too.
AGENT_SURFACES = ("mcp", "api")


def _require_agents_running(held) -> None:
    """Pausing agents stops what they already asked for, not only what they ask next."""

    if held.requested_interface in AGENT_SURFACES and agents_paused():
        raise ApprovalError(
            "Agents are paused, and an agent asked for this. Resume agents to approve it, or decline it."
        )


APPROVE = "approval.approve"
REJECT = "approval.reject"


@dataclass(frozen=True, slots=True)
class ApproveCommand:
    """The held request to agree to."""

    approval_id: Annotated[str, Describe("The held request to agree to.")]


@dataclass(frozen=True, slots=True)
class RejectCommand:
    """The held request to refuse, and what to tell whoever asked."""

    approval_id: Annotated[str, Describe("The held request to refuse.")]
    note: Annotated[str, Describe("What to tell whoever asked.")] = ""


def approve(command: ApproveCommand, *, principal: Principal) -> dict[str, Any]:
    """Agree to a held change and run exactly the call that was held.

    The replay runs under a principal that names whoever asked and who allowed
    it, so the audit trail keeps saying that a token asked and a person agreed.
    Not one transaction end to end: a lapsed or superseded mark commits on its
    own (``_settle``) before the refusal, and only the application is atomic.
    """

    held = _decidable(command.approval_id)
    spec = capabilities.capability_registry().get(held.capability)
    if spec is None:
        raise ApprovalError("What this request asks for is no longer something HQ can do, so it cannot be applied.")
    _require_person(principal, held)
    _require_agents_running(held)
    # The approver has to be allowed to do the thing themselves. Otherwise this
    # page would be a way to run a capability the clicker does not hold.
    authorize_capability(spec, principal)
    command = validate_command(spec, held.payload)
    current = approval_subject(spec, command, held.target or None)
    baseline = current.baseline if current is not None else {}
    if fingerprint(held.capability, held.target or None, held.payload, baseline) != (held.content_fingerprint):
        _settle(
            held,
            ApprovalRequest.State.STALE,
            "What it would change moved after it was requested.",
        )
        raise ApprovalError(
            "This changed after the agent asked, so approving it would apply "
            "something other than what is shown. Ask the agent to request it again."
        )
    acting = Principal(
        held.requested_actor,
        held.requested_interface,
        principal.capabilities,
        approved_by=principal.actor,
    )
    with transaction.atomic():
        # Re-read under a lock. Two people can have this page open, and an
        # approval applied twice is the change made twice, which for a queued
        # operation is caught by its idempotency key and for a declaration is not.
        locked = ApprovalRequest.objects.select_for_update().get(pk=held.pk)
        if locked.state != ApprovalRequest.State.PENDING:
            raise ApprovalError(f"That request is already {locked.get_state_display().lower()}.")
        result = execute_approved(spec, command, locked.target or None, principal=acting)
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


def reject(command: RejectCommand, *, principal: Principal) -> dict[str, Any]:
    """Refuse a held change, so the caller's request has an answer either way."""

    held = _decidable(command.approval_id)
    _require_person(principal, held)
    with transaction.atomic():
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


# ----- What a person is shown -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangePreview:
    """The comparison a decision is taken on.

    ``rows`` is the semantic reading: a document is parsed and compared value
    by value, so an access policy shows the three grants that moved rather than
    two walls of text that differ somewhere. ``lines`` is the honest fallback for content
    that will not parse: a policy document with comments in it, for instance,
    where a unified diff is the most that can truthfully be said.
    """

    label: str
    rows: tuple[ChangeRow, ...]
    lines: tuple[str, ...]
    # Whether the request sets these values on its record, so one that moves
    # is said as the change itself.
    amends: bool = False

    @property
    def empty(self) -> bool:
        return not self.rows and not self.lines

    @property
    def compares(self) -> bool:
        """Whether there is a before to show, or only what would be written."""

        return any(row.change != "added" for row in self.rows)


def _text_fields(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    """Fields that stayed unparsed text on both sides, differ, and run over
    lines. A one-line value is a value, said as a row like any other.
    """

    def text(value: Any) -> bool:
        return isinstance(document(value), str)

    return tuple(
        sorted(
            field
            for field in set(before) | set(after)
            if text(was := before.get(field, ""))
            and text(now := after.get(field, ""))
            and str(was) != str(now)
            and "\n" in f"{was}{now}"
        )
    )


def compare(before: dict[str, Any], after: dict[str, Any], *, label: str, amends: bool = False) -> ChangePreview:
    """The values that differ between two specs, each named and said in words,
    and a line diff where a document will not parse.
    """

    text = _text_fields(before, after)
    structural_before = {key: value for key, value in before.items() if key not in text}
    structural_after = {key: value for key, value in after.items() if key not in text}
    rows = changed_words(structural_before, structural_after)
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
    return ChangePreview(label=label, rows=rows, lines=tuple(lines), amends=amends)


def preview(held: ApprovalRequest) -> ChangePreview:
    """What approving this request would change, as a person needs to read it.

    Two different comparisons, because two different things are being asked.
    Amending a declaration is a change to what HQ intends, so it is read against
    what HQ intends now. Applying one is a change to the world, so it is read
    against what the world was last seen holding, which is the only reading
    that answers "what happens to the network if I click this".
    """

    spec = capabilities.capability_registry().get(held.capability)
    if spec is not None and spec.subject_resource != DECLARATIONS:
        return _record_preview(spec, held)
    declared = dict(held.baseline.get("spec") or {})
    provider = PROVIDERS.get(held.resource_kind)
    requested = held.payload.get("spec")
    if isinstance(requested, dict):
        return compare(declared, requested, label="Would change", amends=True)
    if held.capability.endswith(".remove"):
        return compare(declared, {}, label="Would be removed")
    if held.capability in _lifecycle_capabilities():
        # A start, stop or restart leaves the declaration as it is: what is
        # approved is the act its sentence names.
        return ChangePreview(label="", rows=(), lines=())
    observed = held.baseline.get("observed") or {}
    if provider is not None and provider.from_record is not None and observed:
        try:
            live = provider.from_record(observed)
        except AttributeError, KeyError, TypeError, ValueError:
            live = {}
        if live:
            return compare(live, declared, label="The live record would change")
    return compare({}, declared, label="Would be applied")


def _lifecycle_capabilities() -> frozenset[str]:
    return frozenset(ACTION_CAPABILITY[OperationRequest.Action(verb)] for verb in LIFECYCLE_VERBS)


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
    schemas = capability_schema(spec).get("properties") or {}
    if spec.effect == DESTRUCTIVE_EFFECT:
        return _field_rows(held.baseline, {}, schemas, label="Would be deleted")
    if not held.target:
        # The sentence says what it wants; these are the values it sends.
        return _field_rows({}, fields, schemas, label="Asked with")
    if fields:
        return _field_rows(
            {key: held.baseline.get(key) for key in fields}, fields, schemas, label="Would change", amends=True
        )
    return ChangePreview(label="", rows=(), lines=())


def _field_rows(
    before: dict[str, Any], after: dict[str, Any], schemas: dict[str, Any], *, label: str, amends: bool = False
) -> ChangePreview:
    """Each field that moves, named and valued through the command's schema."""

    rows = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if (was or "") == (now or ""):
            continue
        schema = schemas.get(key)
        rows.append(
            ChangeRow(
                label=field_words(key, schema),
                before=value_words(was, schema),
                after=value_words(now, schema),
                change="added" if was is None else "removed" if now is None else "changed",
            )
        )
    return ChangePreview(label=label, rows=tuple(rows), lines=(), amends=amends)


# Held requests are decided on their audit entry. State stays on ApprovalRequest.


@dataclass(frozen=True, slots=True)
class Asked:
    """What a held request wants done, as the one sentence a person reads it
    by, cut around its target so a page can link it."""

    actor: str
    before: str
    target: str
    after: str
    # Whether it says the whole change, which is then not said again.
    whole: bool = False

    def __str__(self) -> str:
        return f"{self.actor} {self.before}{self.target}{self.after}"


# The longest value a sentence says inline; a longer one is left to the table.
_SAID_INLINE = 60


def asked(held: ApprovalRequest, shown: ChangePreview | None = None) -> Asked:
    """The sentence for ``held``: what its capability says it wants
    (``CapabilitySpec.wanted``), a switch as the side it sets, and a change of
    one value as that change. ``shown`` is its preview, when already read."""

    shown = preview(held) if shown is None else shown
    spec = capabilities.capability_registry().get(held.capability)
    # A switch is said as the side it sets, never as a value moving.
    moved = _one_move(shown) if spec is not None and spec.target_kind and not switch_field(spec) else None
    if moved is not None:
        expected = "expected " if spec.subject_resource == DECLARATIONS else ""
        phrase = f"change the {expected}{lower_first(moved.label)} of {TARGET} from {moved.before} to {moved.after}"
    else:
        phrase = _wanted(spec, held)
    if not held.target:
        phrase = phrase.replace(f" {TARGET}", "").replace(TARGET, "")
    before, _, after = phrase.partition(TARGET)
    return Asked(
        held.requested_actor,
        f"wants to {before}",
        _named(held) if TARGET in phrase else "",
        after,
        whole=moved is not None,
    )


def _named(held: ApprovalRequest) -> str:
    """What a person knows the target by: a record's name, or the name a
    declaration answers for, from what was read when it was asked; its key
    where neither says one."""

    declared = held.baseline.get("spec")
    if held.resource_kind and isinstance(declared, dict):
        return record_name(held.resource_kind, declared, held.target, leads=False)
    read = held.baseline.get("resource", held.baseline)
    return (item_name(read) if isinstance(read, dict) else "") or held.target


def _wanted(spec, held: ApprovalRequest) -> str:
    if spec is None:
        phrase = lower_first(human_label(held.capability))
        return f"{phrase} {TARGET}" if held.target else phrase
    switch = switch_field(spec)
    if switch and isinstance(held.payload.get(switch), bool):
        return spec.wanted("on" if held.payload[switch] else "off")
    return spec.wanted()


def _one_move(shown: ChangePreview) -> ChangeRow | None:
    """The one value an amendment moves, when it moves one short enough to say."""

    if not shown.amends or len(shown.rows) != 1 or shown.lines:
        return None
    (row,) = shown.rows
    said = (row.before, row.after)
    if row.change != "changed" or any(len(value) > _SAID_INLINE or "\n" in value for value in said):
        return None
    return row


def review(held: ApprovalRequest) -> dict[str, Any]:
    """What the decision card shows."""

    shown = preview(held)
    return {
        "held": held,
        "asked": asked(held, shown),
        "preview": shown,
        "warnings": _warnings(held),
        "resource_url": (
            entity_link("resource", held.resource_key).url
            if held.resource_kind and ManagedResource.objects.filter(key=held.resource_key).exists()
            else ""
        ),
    }


def _warnings(held: ApprovalRequest) -> tuple[str, ...]:
    """What the declaration this request would write reaches, said plainly."""

    provider = PROVIDERS.get(held.resource_kind)
    requested = held.payload.get("spec")
    if provider is None or provider.review_warnings is None or not isinstance(requested, dict):
        return ()
    return provider.review_warnings(requested)


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

    return tuple(str(held.pk) for held in pending(limit=None))
