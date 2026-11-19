"""Operations queued against a declared resource.

Reconcile, lifecycle, removal, route approval, reach and certificate renewal:
each is checked against the provider's policy, queued atomically and audited
before the controller runs it.
"""

import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypedDict, cast

from django.db import transaction
from django.db.models import QuerySet

from hq.domains.control_plane.desired_state import advance_dependents
from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND
from hq.domains.control_plane.providers import PROVIDERS, controller_action_policy

from .adoption import (
    NOTHING_LIVE_CHANGED,
    found_live,
    keep_out_declaration,
    keeping,
    observes_only,
    require_writable,
)
from .answer_fields import CallMessage
from .asks import Asked, AskedAnswer, Standing
from .cadence import ask, swept
from .command_fields import Describe
from .conditions import DRIFTED, PROBLEMS, holding
from .doors import NOBODY
from .expiry import certificate_expiry, days_until, renewal_opens_at, renewal_window
from .infrastructure import (
    ManagedResourceCommand,
    delivery_targets,
    save_adopted,
    save_managed_resource,
)
from .record_errors import PolicyError, RecordNotFound, Sentence
from .record_saves import Created, row
from .resource_records import (
    AskedAt,
    AttemptCount,
    CompletedAt,
    ConditionPart,
    ManagedResourceItem,
    OperationAction,
    OperationId,
    OperationReason,
    OperationState,
    OperationSummary,
    RequestedActor,
    RequestedInterface,
)
from .security import CONTROLLER_INTERFACE, Principal
from .ui import counted, ended
lazy from . import tailnet
lazy from .capabilities import Consented, capability_words, consent
lazy from .containers import Running
lazy from .history import asked_by
lazy from .integration_specs import requested
lazy from .machines import Machine, container_context
lazy from .moments import when_day
lazy from .resource_capabilities import public_dns_enabled
lazy from .tailnet import POLICY_KIND, device_at, may_reach, observer, policy_allowing
lazy from .work_runs import WorkRun, run_rows, seen_as_runs, shown_result, standing_of


@dataclass(frozen=True, slots=True)
class OperationCommand:
    reason: Annotated[str, Describe("Why it is done, kept with the operation it queues.")] = ""


class OperationItem(TypedDict):
    """One operation asked of the controller, as it stands."""

    id: OperationId
    resource: Annotated[str, Describe("The key of the record it acts on.")]
    action: OperationAction
    state: OperationState
    reason: OperationReason
    requested_actor: RequestedActor
    requested_interface: RequestedInterface
    created_at: AskedAt
    completed_at: CompletedAt
    claimed_by: Annotated[str, Describe("The controller working on it; empty while none is.")]
    lease_expires_at: Annotated[
        str | None,
        Describe("When the controller's hold on it lapses, an ISO 8601 date-time; null while none holds it."),
    ]
    attempt_count: AttemptCount
    result: Annotated[dict[str, Any], Describe("What the controller reported of its run; empty until it has run.")]
    correlation: Annotated[str, Describe("The asker's own id for finding it later; empty for none.")]


# The reconcile a save queues, which every answer built on a save carries.
type Applying = Annotated[
    OperationItem | None, Describe("The operation queued to apply the change at once; null where none was.")
]


class ResourceSaved(TypedDict):
    """A managed record as saved, and the reconcile queued to apply it."""

    ok: Literal[True]
    created: Created
    resource: ManagedResourceItem
    applying: Applying
    message: CallMessage


type Queued = Annotated[bool, Describe("Whether this call queued it; false when the same one was already waiting.")]


class OperationAsked(AskedAnswer):
    """An operation asked of a worker, as every ask answers: how it stands,
    and the operation, and whether this call queued it."""

    queued: Queued
    operation: OperationItem


class RunAsked(OperationAsked):
    """A playbook's run as asking for it or cancelling it answers: the run
    that `runs` reads by its `operation.id`."""


def operation_asked(
    operation: OperationRequest,
    *,
    queued: bool,
    message: str = "",
    standing: Standing | None = None,
    status_url: str = "",
) -> OperationAsked:
    """What asking for ``operation`` answers: how it stands as a list of work
    says it (``work_runs.standing_of``) unless the caller words it, and
    ``status_url`` where a page can follow it."""

    if standing is None:
        seen = seen_as_runs(OperationRequest.objects.filter(pk=operation.pk).select_related("resource")).get()
        standing = standing_of(run_rows((seen,))[0])
    asked = Asked(standing, status_url, message).answer()
    return {**asked, "queued": queued, "operation": serialize_operation(operation)}


class DeclarationForgotten(TypedDict):
    """A record HQ stopped managing, left as it is at its provider."""

    ok: Literal[True]
    queued: Annotated[
        Literal[False], Describe("That nothing was queued, since the service holding it is left as it is.")
    ]
    forgotten: Annotated[str, Describe("The key of the record HQ stopped managing.")]
    released: Annotated[list[str], Describe("The keys of the records inside it that HQ stopped managing too.")]
    reason: OperationReason
    message: CallMessage


class ObservedAccepted(ResourceSaved):
    """The record as saved from what its provider holds, and why that was asked."""

    reason: OperationReason


def serialize_operation(operation: OperationRequest) -> OperationItem:
    who = asked_by(OperationRequest, (operation,)).get(str(operation.pk), NOBODY)
    return row(
        operation,
        OperationItem,
        id=str(operation.id),
        resource=operation.resource.key,
        requested_actor=who.actor,
        requested_interface=who.interface,
        correlation=str((operation.input or {}).get("correlation", "")),
    )


# How many past operations are read to find the ones that changed something,
# and how many of those a record's history shows.
HISTORY_WINDOW = 200
HISTORY_ROWS = 20


def operation_summary(run: WorkRun, operation: OperationRequest) -> OperationSummary:
    """One operation as its run (``work_runs.run_rows``) says it, with what
    only an operation has: its action, the condition its result carries, and
    what each name served of the certificate it checked."""

    who = run.by
    return {
        "id": run.id,
        "action": operation.action,
        "action_label": operation.get_action_display(),
        "state": operation.state,
        "state_label": run.word,
        "tone": run.tone,
        "headline": run.note,
        "automatic": who.interface == CONTROLLER_INTERFACE,
        "requested_actor": who.actor,
        "requested_interface": who.interface,
        "by": who.words,
        # Blank for almost everything, and the point when it is not: this is the
        # person who agreed to a change a credential asked for.
        "approved_by": who.approved_by,
        "created_at": run.created_at.isoformat(),
        "completed_at": (run.finished.isoformat() if run.finished else None),
        "attempt_count": operation.attempt_count,
        "reason": operation.reason,
        # A condition the controller reported is the part's shape.
        "condition": cast(ConditionPart | None, next(iter(holding((operation.result or {}).get("conditions"))), None)),
        "affected": list(run.evidence.affected),
        "evidence": list(run.evidence.consumers),
        "matched": run.evidence.matched,
        "expected_fingerprint_sha256": run.evidence.expected,
        "raw_result": shown_result(operation.result or {}),
    }


def summaries(operations: Iterable[OperationRequest]) -> list[OperationSummary]:
    """``operations``, read through ``work_runs.seen_as_runs``, each as ``operation_summary`` says it."""

    found = list(operations)
    return [operation_summary(run, operation) for run, operation in zip(run_rows(found), found, strict=True)]


def changes(operations: QuerySet[OperationRequest], limit: int) -> list[OperationRequest]:
    """The operations worth a row of history, from the latest ``HISTORY_WINDOW``
    of ``operations`` newest first, as ``work_runs.run_rows`` reads them.

    A check that runs every minute is one row until its answer changes.
    """

    # An automatic run that ended exactly as the run before it on the same
    # record (same state, same sentence, same conditions) repeats what is
    # already listed, so it is left out. Anything a person or an agent asked
    # for, anything unfinished and anything that failed is always listed.
    def signature(operation: OperationRequest) -> tuple[Any, ...]:
        result = operation.result or {}
        return (
            operation.state,
            result.get("message", ""),
            tuple(
                (item.get("type"), item.get("reason"), item.get("message"))
                for item in holding(result.get("conditions"))
            ),
        )

    last: dict[tuple[Any, str], tuple[Any, ...]] = {}
    kept: list[OperationRequest] = []
    for operation in reversed(list(seen_as_runs(operations.select_related("resource"))[:HISTORY_WINDOW])):
        key = (operation.resource_id, operation.action)
        said = signature(operation)
        repeats = (
            operation.requested_interface == CONTROLLER_INTERFACE
            and operation.state == OperationRequest.State.SUCCEEDED
            and last.get(key) == said
        )
        last[key] = said
        if not repeats:
            kept.append(operation)
    kept.reverse()
    return kept[:limit]


def resource_history(resource: ManagedResource) -> list[OperationSummary]:
    """One record's history: each operation that changed something, newest first."""

    return summaries(changes(resource.operations.all(), HISTORY_ROWS))


def refuse_while_drifted(resource: ManagedResource) -> None:
    """Refuse to amend a declaration the provider no longer matches.

    A remedy that edits a declaration edits HQ's copy, and applying it pushes
    that whole copy. While the live record differs, whatever changed there
    would be overwritten along with the one intended change, so the operator
    decides first: keep the live version, or restore HQ's.
    """

    if holding(resource.conditions, DRIFTED):
        raise PolicyError("This record differs from what is live. Keep the live version or restore HQ's first.")


def _resource_for_operation(key: str) -> ManagedResource:
    try:
        return ManagedResource.objects.select_for_update().get(key=key)
    except ManagedResource.DoesNotExist as exc:
        raise RecordNotFound("That record was not found.") from exc


# The key of a reconcile's input holding the copy of a declaration an apply by
# hand may only replace.
REPLACES = "replaces"


type Prepared = tuple[ManagedResource, dict[str, Any]]


def enqueue(
    action: str,
    prepare: Callable[[], Prepared],
    *,
    once: str = "",
) -> tuple[OperationRequest, bool]:
    """The one way an operation enters the queue: once per record and action
    while one waits, and once for ``once``. Returns it and whether this call
    queued it.

    ``once`` is what HQ's own work is queued once for (a generation, a
    finding's evidence, a day's renewal), so the operation it already queued
    answers before ``prepare`` checks anything. A caller's retry key is the
    capability runner's (``execute_once``), never the row's. Otherwise
    ``prepare`` checks the request and returns the record and the row's
    fields. The key's unique column decides between two writers on one key:
    ``get_or_create`` hands the second one the first one's row.
    """

    key = once or f"{action}:{secrets.token_urlsafe(18)}"
    found = OperationRequest.objects.filter(idempotency_key=key).first()
    if found is not None:
        return found, False
    resource, fields = prepare()
    active = OperationRequest.objects.filter(
        resource=resource,
        action=action,
        state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED),
    ).first()
    if active is not None:
        return active, False
    operation, created = OperationRequest.objects.get_or_create(
        idempotency_key=key, defaults={"resource": resource, "action": action, **fields}
    )
    return operation, created


def asked_fields(principal: Principal, *, reason: str, **asked: Any) -> dict[str, Any]:
    """A queued operation's fields: who asked, why, and its ``input``.

    Who asked stays who asked. Where a person had to agree before it could be
    queued, that is recorded beside the request rather than in place of it:
    an operation applied on somebody's say-so and one a credential queued
    alone must not read the same afterwards.
    """

    return {
        "requested_actor": principal.actor,
        "requested_interface": principal.interface,
        "requested_device": principal.device,
        "requested_person_key": principal.person_key,
        "reason": reason,
        "input": {**asked, **({"approved_by": principal.approved_by} if principal.approved_by else {})},
    }


def _queue_operation(
    resource: ManagedResource,
    command: OperationCommand,
    *,
    consented: Consented,
    action: str,
    require_enabled: bool = True,
    replaces: dict[str, Any] | None = None,
    once: str = "",
) -> OperationAsked:
    def prepare() -> Prepared:
        if require_enabled and not resource.enabled:
            raise PolicyError("This record is switched off in HQ.")
        require_writable(resource.kind, resource.spec)
        # Nothing enters the queue for a gated kind without a person behind
        # it. The controller's own automatic work does not pass through here
        # (it queues as itself, converging toward a declaration somebody has
        # already agreed to) so this refuses exactly the case it is about: a
        # credential asking for the world to be changed.
        consent(consented).require(resource.kind)
        allowed, explanation = controller_action_policy(resource.kind, action)
        if not allowed:
            raise PolicyError(explanation)
        # The copy an apply by hand may only replace (``controller.over_live``).
        replaced = {REPLACES: replaces} if replaces is not None else {}
        return resource, asked_fields(
            consent(consented).principal, reason=command.reason, generation=resource.generation, **replaced
        )

    operation, queued = enqueue(action, prepare, once=once)
    said = requested(capability_words(ACTION_CAPABILITY[action]), resource.key, again=not queued)
    if not queued:
        return operation_asked(operation, queued=False, message=said)
    # Every operation a person or credential asks for is created here, a
    # capability's or not, so this rings for all of them; with the runner's
    # ring in the same commit it is one ring.
    ask()
    return operation_asked(operation, queued=True, message=said)


def queue_reconcile(
    resource: ManagedResource, *, consented: Consented, reason: str, replaces: dict[str, Any] | None = None
) -> OperationAsked:
    """Queue a reconcile of a declaration just written, as the write's own act.

    For a service that writes a declaration under its own capability (the
    contact inbox under contacts), so it is checked against the same policy
    as any other reconcile. Keyed on the generation: asking twice for one
    generation queues it once. ``replaces`` is the copy the write replaced,
    which the claim holds the live record to.
    """

    return _queue_operation(
        resource,
        OperationCommand(reason=reason),
        consented=consented,
        action=OperationRequest.Action.RECONCILE,
        replaces=replaces,
        once=f"reconcile:{resource.pk}:g{resource.generation}",
    )


@transaction.atomic
def request_reconcile(
    command: OperationCommand,
    *,
    consented: Consented,
    current_key: str,
) -> OperationAsked:
    resource = _resource_for_operation(current_key)

    if PROVIDERS[resource.kind].public_effect and not public_dns_enabled():
        raise PolicyError("Public DNS changes are off on this server.")
    return _queue_operation(
        resource,
        command,
        consented=consented,
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
) -> DeclarationForgotten:
    """Stop being responsible for something without touching the provider.

    For a declaration-only kind (a domain) and for anything whose connection
    only observes. Nothing is queued and nothing is deleted at the provider.
    """

    # The declarations inside it go too. Left behind, HQ would keep
    # reconciling records in a domain it is no longer responsible for. They
    # are forgotten rather than deleted: the records stay exactly as they
    # are at the provider, which is what stepping back means.
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
    inside = f" and {counted(len(contained), 'record')} in it" if contained else ""
    return {
        "ok": True,
        "queued": False,
        "forgotten": key,
        "released": contained,
        "reason": command.reason,
        "message": f"{ended(f'Stopped tracking “{key}”{inside}')} {NOTHING_LIVE_CHANGED}",
    }


@transaction.atomic
def _request_lifecycle(
    command: OperationCommand,
    *,
    consented: Consented,
    current_key: str,
    action: str,
) -> OperationAsked:
    """Ask the controller to start, stop or restart what a declaration names.

    Not reconciliation: cycling a container is asked for once, about something
    already exactly as declared, so it neither bumps the generation nor waits
    on one.
    """

    resource = _resource_for_operation(current_key)
    gap = unit_gap(resource)
    if gap:
        raise PolicyError(gap)
    # Which verbs exist is the capability registry's to say, so a verb the
    # controller does not implement is refused here rather than queued for
    # work nothing will claim.
    result = _queue_operation(resource, command, consented=consented, action=action)
    # The page that asked shows the state the sweep last read, so the run that
    # applies this reads the kind again after it, rather than leaving the old
    # state up until the cadence comes round. Only a kind the sweep reads:
    # nothing would answer a request for any other.
    if result["queued"] and swept(resource.kind):
        ask(reads=(resource.kind,))
    return result


# Why a container its unit runs is not stopped, started or restarted from HQ:
# the controller asks a unit only through root's socket on its own machine.
UNIT_OUT_OF_REACH: Sentence = "A systemd unit runs this container, and HQ asks a unit only on the machine HQ runs on."


@dataclass(frozen=True, slots=True)
class Supervision:
    """The systemd unit that runs a container, as its label was last read."""

    unit: str
    # Whether HQ reaches the unit: only on the machine HQ runs on.
    reachable: bool


def supervision(resource: ManagedResource) -> Supervision | None:
    """The unit that runs the container a declaration names, or None for a
    container no unit runs (or one not read yet)."""

    spec = resource.spec or {}
    context = container_context(str(spec.get("host", "")), str(spec.get("name", "")))
    running, machine = context.get("running"), context.get("machine")
    if not isinstance(running, Running) or not running.unit:
        return None
    return Supervision(unit=running.unit, reachable=isinstance(machine, Machine) and machine.runs_hq)


def unit_gap(resource: ManagedResource) -> Sentence:
    """Why a lifecycle verb cannot be asked of this container, or ""."""

    found = supervision(resource)
    return UNIT_OUT_OF_REACH if found is not None and not found.reachable else ""


def request_restart(command: OperationCommand, *, consented: Consented, current_key: str) -> OperationAsked:
    return _request_lifecycle(
        command, consented=consented, current_key=current_key, action=OperationRequest.Action.RESTART
    )


def request_start(command: OperationCommand, *, consented: Consented, current_key: str) -> OperationAsked:
    return _request_lifecycle(
        command, consented=consented, current_key=current_key, action=OperationRequest.Action.START
    )


def request_stop(command: OperationCommand, *, consented: Consented, current_key: str) -> OperationAsked:
    return _request_lifecycle(
        command, consented=consented, current_key=current_key, action=OperationRequest.Action.STOP
    )


# The variant of reconcile that a record only reporting on itself offers: reading it again.
READS_AGAIN = "reads_again"

# The capability that asks for each verb, so a page asks for one by its verb
# and runs it through the capability runner like every other caller. A run is
# asked through its procedure's own command (``automation_runs.capability_name``).
ACTION_CAPABILITY = {
    OperationRequest.Action.RECONCILE: "infrastructure.resource.reconcile",
    OperationRequest.Action.RENEW: "certificate.renew",
    OperationRequest.Action.DELETE: "infrastructure.resource.remove",
    OperationRequest.Action.RESTART: "infrastructure.resource.restart",
    OperationRequest.Action.START: "infrastructure.resource.start",
    OperationRequest.Action.STOP: "infrastructure.resource.stop",
    OperationRequest.Action.APPROVE_ROUTES: "tailnet.routes.approve",
}


@transaction.atomic
def accept_observed(
    command: OperationCommand,
    *,
    consented: Consented,
    current_key: str,
) -> ObservedAccepted:
    """Make the declaration say what the provider holds.

    For a change made at the provider on purpose (a policy a connector edited,
    a setting changed in its own console), which reconciling would undo.
    Nothing is written to the provider. The controller's report is evidence
    of what exists, never consent to keep it, so a kind that waits for a
    person is accepted only under one's consent.
    """

    resource = _resource_for_operation(current_key)
    consent(consented).require(resource.kind)
    # Copied from the live record, the same way adopting does, so the next
    # sweep finds the two agreeing.
    found = found_live(resource.kind, resource.spec)
    if found is None:
        raise RecordNotFound("No live record was last seen for this one, so there is nothing to accept.")
    # A field no sweep can observe (hidden, on demand) stays as it was declared.
    result = save_adopted(
        keeping(found, resource.spec),
        key=resource.key,
        enabled=resource.enabled,
        principal=consent(consented).principal,
        current_key=resource.key,
    )
    return {**result, "reason": command.reason}


def request_removal(
    command: OperationCommand,
    *,
    consented: Consented,
    current_key: str,
) -> OperationAsked | DeclarationForgotten:
    """Queue removal of the thing this declaration describes.

    Not a plain row delete: the record lives at a provider, so forgetting the
    declaration would orphan it there. HQ drops its own row only once a
    controller reports the provider is clear.
    """

    resource = _resource_for_operation(current_key)
    # HQ deletes at the provider only through a connection that manages.
    if PROVIDERS[resource.kind].declaration_only or observes_only(resource.kind, resource.spec):
        return _forget_declaration(resource, command, principal=consent(consented).principal)
    allowed, explanation = controller_action_policy(resource.kind, OperationRequest.Action.DELETE)
    if not allowed:
        raise PolicyError(explanation)
    # Bypasses the enabled check in _queue_operation on purpose: that guard
    # exists to stop HQ converging a paused declaration, and removal is the
    # opposite of converging it. A switched-off record is exactly what is
    # left just before an operator decides to be rid of it.
    return _queue_operation(
        resource,
        command,
        consented=consented,
        action=OperationRequest.Action.DELETE,
        require_enabled=False,
    )


def certificate_renewal_allowed(resource: ManagedResource) -> tuple[bool, Sentence]:
    if resource.kind != CERTIFICATE_KIND:
        return False, "Only a certificate HQ issues can be renewed."
    if not resource.enabled:
        return False, "The certificate is switched off in HQ."
    allowed, explanation = controller_action_policy(resource.kind, OperationRequest.Action.RENEW)
    if not allowed:
        return False, explanation
    if holding(resource.conditions, *PROBLEMS):
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

    return (
        False,
        (
            f"Renews automatically from {when_day(renewal_opens_at(expiry, window))} "
            f"({counted(window, 'day')} before it expires)."
        ),
    )


@transaction.atomic
def request_route_approval(
    command: OperationCommand,
    *,
    consented: Consented,
    current_key: str,
) -> OperationAsked:
    """Approve the routes a tailnet device is already advertising.

    The remedy the "advertises routes nothing approved" finding names. HQ does
    not choose the routes: the machine has already said what it offers, and
    this is the consent that turns that offer into something the tailnet hands
    out. Which is why it is never automatic: approving a route is a decision
    to trust that machine with traffic for those addresses.
    """

    resource = _resource_for_operation(current_key)
    return _queue_operation(
        resource,
        command,
        consented=consented,
        action=OperationRequest.Action.APPROVE_ROUTES,
    )


def request_reach_allow(
    _command: OperationCommand,
    *,
    consented: Consented,
    current_key: str,
) -> ResourceSaved:
    """Open the path a reading proved is shut, where the tailnet is what shut it.

    Given a resource, never a policy, so the only change this can produce is
    one an observation already justified; a person consents to it before it
    reaches the tailnet.
    """

    resource = _resource_for_operation(current_key)

    # Which path to open is re-derived from what the controller observed (the
    # address and port it could not reach), so a caller cannot name one.
    known = tailnet.devices()
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
        except ValueError:  # noqa: S112 - a consumer with no port number has no port to open
            continue
        if target is None or not port:
            continue
        verdict = may_reach(watcher.name, target.name, port, known)
        if verdict.known and not verdict.allowed:
            shut.append((target.name, port))
    if not shut:
        raise PolicyError(
            "The tailnet policy blocks none of the places that failed, so allowing more will not fix this."
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

    # The ordinary write, which puts it in front of a person: the tailnet
    # policy is a gated kind, so this proposes and somebody else consents.
    return save_managed_resource(
        ManagedResourceCommand(
            key=policy.key,
            kind=policy.kind,
            spec={**policy.spec, "document": document},
            enabled=policy.enabled,
        ),
        consented=consented,
        current_key=policy.key,
    )


def request_certificate_renewal(
    command: OperationCommand,
    *,
    consented: Consented,
    current_key: str,
) -> OperationAsked:
    resource = _resource_for_operation(current_key)
    allowed, explanation = certificate_renewal_allowed(resource)
    if not allowed:
        raise PolicyError(explanation)
    return _queue_operation(resource, command, consented=consented, action=OperationRequest.Action.RENEW)
