"""Deterministic JSON capability registry for every HQ adapter."""

import secrets
from collections.abc import Callable
from dataclasses import replace
from typing import Annotated, Any, Literal, NamedTuple, TypedDict

from django.db import transaction
from pydantic import ConfigDict, with_config

from hq.platform.core.audit import audit_connection, audit_operation
lazy from hq.platform.core.doorbell import ask_soon

from .answer_fields import Permitted, Pinned, RequiredPermissions, SchemaVersion
from .approvals import hold_for_approval
from .call_envelope import read_call
from .capability_policy import Rule, decide
from .contracts import DOTTED_NAME, EFFECT_MEANINGS
from .describe import Describe, listed
from .held_answer import HeldCall
from .idempotency import (
    KEY_SHAPE,
    IdempotencyConflict,
    InvalidIdempotencyKey,
    execute_once,
    request_fingerprint,
    validate_key,
)
from .integration_specs import (
    IDEMPOTENCY_FIELD,
    SENT_KEYWORD,
    STALE_WRITE_KEYWORD,
    TARGET_KINDS,
    CapabilitySpec,
    capability_schema,
    command_adapter,
    declares_idempotency_key,
    kept_by,
    takes_sent_fields,
    takes_stale_write_token,
)
from .integrations import integration_graph
from .labels import human_label
from .projection import QueryRefusal
from .refusal_status import Refusal, next_call, refusal_of, refuse
from .security import (
    CLI_INTERFACE,
    Capability,
    PolicyDenied,
    Principal,
    could_ever_hold,
    require_all,
)
from .ui import ended
lazy from . import agent_registry


def capability_title(name: str) -> str:
    """What a person calls the command named ``name``, registered or not."""

    spec = capability_registry().get(name)
    return spec.title if spec else human_label(name)


def capability_words(name: str, variant: str = "") -> str:
    """What a button for the command named ``name`` says (``CapabilitySpec.words``)."""

    return capability_registry()[name].words(variant)


def capability_registry() -> dict[str, CapabilitySpec]:
    return dict(integration_graph().capabilities)


def authorize_capability(spec: CapabilitySpec, principal: Principal) -> None:
    """Apply the registry's one authorization rule for every adapter."""

    require_all(principal, spec.required_capabilities)


class CapabilityDescription(TypedDict):
    """One registry entry as every adapter describes it."""

    name: Annotated[str, Describe("The command's dotted name, which running it takes.")]
    label: Annotated[str, Describe("What a person calls the command.")]
    verb: Annotated[str, Describe("What its button says.")]
    variants: Annotated[
        dict[str, str],
        Describe("What its button says in each of its modes, by the mode's name, where one button cannot say all."),
    ]
    summary: Annotated[str, Describe("What the command does, in a sentence.")]
    effect: Annotated[str, Describe(ended(f"What running it does: {listed(EFFECT_MEANINGS)}"))]
    required_capabilities: RequiredPermissions
    target: Annotated[
        str | None,
        Describe(f"What names the record it acts on: {listed(TARGET_KINDS)}; null for a command that takes none."),
    ]
    target_label: Annotated[str, Describe("What a person calls the record it acts on; empty for none.")]
    target_help: Annotated[str, Describe("What to send as the target, in a sentence; empty for none.")]
    target_query: Annotated[
        dict[str, str | int | float | bool],
        Describe("What a record must hold to be offered as its target, value by field; empty for any record."),
    ]
    execution_notes: Annotated[list[str], Describe("What running it does, step by step, a sentence each.")]
    target_initial_fields: Annotated[
        list[str], Describe("The fields of its command filled in from the target's record, to be changed from there.")
    ]
    resource: Annotated[
        str | None,
        Describe("The list its targets are read from, by the name `describe_resources` gives it; null for none."),
    ]
    input_schema: Annotated[dict[str, Any], Describe("The JSON Schema of the command it takes as `payload`.")]
    permitted: Permitted


class InvalidCapabilityQuery(QueryRefusal):
    """A narrowing input the capability catalog cannot be read by; ``reason`` says which."""


def _narrowed(name: str, resource: str) -> list[CapabilitySpec]:
    """The capabilities under the dotted ``name`` and about ``resource``, refusing what names nothing.

    ``name`` is a whole capability name or a prefix of whole segments
    (``infrastructure`` holds ``infrastructure.resource.reconcile``), so a filter never
    matches half a word.
    """

    graph = integration_graph()
    if name and not DOTTED_NAME.match(name):
        raise InvalidCapabilityQuery("capabilities: name is a dotted capability name or a prefix of one.")
    if resource and resource not in graph.resources:
        raise InvalidCapabilityQuery("That list name matches nothing HQ serves; describe_resources names each one.")
    return [
        spec
        for spec in graph.capabilities.values()
        if (not name or spec.name == name or spec.name.startswith(f"{name}."))
        and (not resource or spec.subject_resource == resource)
    ]


# The shape describe_capabilities answers in; it moves when that shape does.
CAPABILITIES_SCHEMA_VERSION = 2
# That version as every door declares it: the one number the catalog carries.
CapabilitiesVersion = Annotated[SchemaVersion, Pinned(CAPABILITIES_SCHEMA_VERSION)]


class CapabilityCatalog(TypedDict):
    """What ``describe_capabilities`` answers."""

    schema_version: CapabilitiesVersion
    capabilities: Annotated[
        list[CapabilityDescription], Describe("Each command asked for that the caller's interface can run.")
    ]


def describe_capabilities(*, principal: Principal, name: str = "", resource: str = "") -> CapabilityCatalog:
    """Return stable JSON Schemas and operational effects for every capability
    ``principal``'s interface could ever hold, or those under a dotted ``name``
    and about a ``resource``, each saying whether ``principal`` is ``permitted`` it.

    A capability its interface never holds is left out, whatever the grant: a
    token is not shown a command only a person can run.
    """

    described: list[CapabilityDescription] = [
        {
            "name": spec.name,
            "label": spec.title,
            "verb": spec.words(),
            "variants": dict(spec.variants),
            "summary": spec.summary,
            "effect": spec.effect,
            "required_capabilities": [
                capability.value if isinstance(capability, Capability) else capability
                for capability in spec.required_capabilities
            ],
            "target": spec.target_kind,
            "target_label": spec.target_label,
            "target_help": spec.target_help,
            "target_query": dict(spec.target_query),
            "execution_notes": list(spec.execution_notes),
            "target_initial_fields": list(spec.target_initial_fields),
            "resource": spec.subject_resource,
            "input_schema": capability_schema(spec),
            "permitted": principal.permits(*spec.required_capabilities),
        }
        for spec in _narrowed(name.strip(), resource.strip())
        if could_ever_hold(principal, spec.required_capabilities)
    ]
    return {"schema_version": CAPABILITIES_SCHEMA_VERSION, "capabilities": described}


def _target_keyword(spec: CapabilitySpec, target: str | int | None) -> dict[str, Any]:
    """Bind the target to the keyword its capability declared it under."""

    if not spec.target_kind:
        return {}
    # ``read_call`` has held the target to its kind, so it coerces.
    kind = TARGET_KINDS[spec.target_kind]
    return {kind.keyword: kind.coerce(target)}


def execute_capability(
    name: str,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Validate JSON and execute one allowlisted application capability."""

    return run_capability(
        name, payload, principal=principal, target=target, expected_updated_at=expected_updated_at
    ).answer


@with_config(ConfigDict(extra="allow"))
class Performed(TypedDict):
    """A capability that ran, as its runner answers: ``ok``, beside what its command answers."""

    ok: Literal[True]


class Ran(NamedTuple):
    """A capability's answer, and whether it is the first answer to its retry key, replayed."""

    answer: dict[str, Any]
    replayed: bool = False


def run_capability(
    name: str,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None = None,
    expected_updated_at: str | None = None,
    idempotency_key: str | None = None,
) -> Ran:
    """Run one capability: the one place a retry key is read, required and kept.

    Every door runs a capability here or through ``execute_capability``, which
    is this with the answer alone: that is the signature ``hq_sdk`` publishes to
    the extensions, while a door also hands over the key it carries beside the
    payload (the API's header, a form's hidden field) and says whether the
    answer was replayed. A key in the payload's ``idempotency_key`` is the same
    key sent in the command.
    """

    spec = capability_registry().get(name)
    if spec is None:
        return Ran(
            refuse(
                "unknown_capability",
                f"Unknown capability {name!r}. describe_capabilities lists the ones this caller may run.",
                {"next": next_call("describe_capabilities")},
            )
        )
    try:
        return _authorized_run(
            spec,
            payload,
            principal=principal,
            target=target,
            expected_updated_at=expected_updated_at,
            sent_key=idempotency_key,
        )
    except InvalidIdempotencyKey as exc:
        return Ran(refuse("invalid_idempotency_key", exc.reason))
    except Exception as exc:  # noqa: BLE001 - every other failure is answered in the one refusal shape
        return Ran(adapter_refusal(name, exc, principal=principal))


def adapter_refusal(name: str, exc: Exception, *, principal: Principal | None = None) -> dict[str, Any]:
    """What an adapter answers for ``exc``, raised running ``name``: a code, and only HQ's own words.

    The one policy every adapter applies to an exception it caught, a
    capability's and an MCP tool's alike (``refusal_status.refusal_of``).
    """

    return refusal_of(exc, name=name, principal=principal).answer()


def _authorized_run(
    spec: CapabilitySpec,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None,
    expected_updated_at: str | None,
    sent_key: str | None,
) -> Ran:
    """Check authority, the target, the retry key, shape and consent, then run once under the caller's key."""

    # Authority first, then the target, the key and the payload. A caller who
    # may not run this at all is told exactly that, and learns nothing about
    # what target it would have taken.
    authorize_capability(spec, principal)
    refused = _target_refusal(spec, target, expected_updated_at)
    if refused is not None:
        return Ran(refused)
    key, payload = _retry_key(spec, payload, sent_key)
    if not key and key_required(spec.effect, principal):
        # A retry of a change sent without a key could act twice.
        return Ran(
            refuse(
                "idempotency_key_required",
                "A retry key is required for capabilities that change state: send a new one as "
                "payload.idempotency_key, and the same one again when you retry this change.",
                {"field": "payload.idempotency_key"},
            )
        )
    command = validate_command(spec, payload, target)
    # A held request has acted on nothing, so it is answered afresh each
    # time: the same key runs the command once a person has agreed.
    held, acting = _consent(spec, payload, command, target, principal)
    if held is not None:
        return Ran({**held})

    def act() -> dict[str, Any]:
        return _run(
            spec,
            command,
            principal=acting,
            target=target,
            expected_updated_at=expected_updated_at,
            sent=frozenset(payload),
        )

    if not key:
        return Ran(act())
    try:
        return _once(
            spec,
            key,
            {"command": payload, "target": target, "expected_updated_at": expected_updated_at},
            act,
            actor=principal.actor,
        )
    except IdempotencyConflict as exc:
        return Ran(refuse("idempotency_conflict", exc.reason))


def key_required(effect: str, principal: Principal) -> bool:
    """Whether ``principal`` must send a retry key to run a capability of ``effect``: a change, from any door but the shell's.

    A token's client retries what a lost connection left unanswered, and a
    browser resends a form (``web_writes``). An allowlist of the one door not
    asked, so a door added later is asked, as ``INTERACTIVE_INTERFACES`` is
    built the safe way round.
    """

    return effect != "read" and principal.interface not in KEYLESS_INTERFACES


# Not asked for a key: the host's shell, where a person runs a command once, by hand.
KEYLESS_INTERFACES = frozenset({CLI_INTERFACE})


def _retry_key(spec: CapabilitySpec, payload: dict[str, Any], sent: str | None) -> tuple[str, dict[str, Any]]:
    """``(key, payload)``: the caller's retry key, and the payload its command reads.

    The key arrives beside the payload (``sent``), in it, or both alike; an
    empty one is none. A read takes none, so one in its payload is an unknown
    field. A command type that declares the field stores it with the operation
    it queues, so it receives the caller's key, or a fresh one when the caller
    sent none. Any other command never sees the field.
    """

    if spec.effect == "read" or not isinstance(payload, dict):
        return "", payload
    keys = {_valid_key(given) for given in (sent, payload.get(IDEMPOTENCY_FIELD))} - {""}
    if len(keys) > 1:
        raise InvalidIdempotencyKey("Two different retry keys were sent with one request.")
    key = keys.pop() if keys else ""
    rest = {name: value for name, value in payload.items() if name != IDEMPOTENCY_FIELD}
    if declares_idempotency_key(spec.command_type):
        rest[IDEMPOTENCY_FIELD] = key or f"command:{secrets.token_urlsafe(18)}"
    return key, rest


def _valid_key(given: object) -> str:
    """``given`` as a retry key, or "" for none; raises ``InvalidIdempotencyKey`` for any other value."""

    if given is None or given == "":
        return ""
    if not isinstance(given, str):
        raise InvalidIdempotencyKey(KEY_SHAPE)
    return validate_key(given)


def _once(
    spec: CapabilitySpec,
    key: str,
    request: dict[str, Any],
    act: Callable[[], dict[str, Any]],
    *,
    actor: str,
) -> Ran:
    """Run ``act`` under the caller's key, or return what it returned the first time.

    The key belongs to the actor and to one request: the same key with another
    capability, target or payload is a conflict. A call that raises, or answers
    with a refusal of its own, keeps nothing, so its key is free for the
    corrected request.
    """

    result, replayed = execute_once(
        actor=actor,
        key=key,
        request_sha256=request_fingerprint(spec.name, request),
        operation=act,
    )
    return Ran(result, replayed)


def _target_refusal(
    spec: CapabilitySpec, target: str | int | None, expected_updated_at: str | None
) -> dict[str, Any] | None:
    """The error for a target the capability requires and lacks or refuses, or for what the call carries.

    The one read of every door's target and stale-write token (``read_call``).
    Whether a target belongs is the capability's alone, so it is asked here
    rather than of the envelope, whose schema every capability shares.
    """

    if spec.target_kind and target is None:
        return refuse(
            "target_required",
            f"{spec.name} requires a target: send the record it acts on as target.",
            {"field": "target"},
        )
    if not spec.target_kind and target is not None:
        return refuse(
            "target_not_allowed",
            f"{spec.name} does not accept a target: send the call without one.",
            {"field": "target"},
        )
    sent = {"target": target, "expected_updated_at": expected_updated_at}
    call = read_call(spec, {field: value for field, value in sent.items() if value is not None})
    return call.answer() if isinstance(call, Refusal) else None


def validate_command(spec: CapabilitySpec, payload: dict[str, Any], target: str | int | None = None) -> Any:
    """The command ``payload`` becomes: what runs, and so what policy judges.

    A write that keeps what it is not sent of the record ``target`` names
    (``Kept``) is that record with ``payload`` laid over it, validated whole,
    so a rule that reads two fields reads them as the record will hold them.
    """

    kept = kept_by(spec) if target is not None else None
    if kept is not None:
        (current,) = _target_keyword(spec, target).values()
        payload = {**kept.read(current), **payload}
    return command_adapter(spec.command_type).validate_python(payload)


def _consent(
    spec: CapabilitySpec,
    payload: dict[str, Any],
    command: Any,
    target: str | int | None,
    principal: Principal,
) -> tuple[HeldCall | None, Principal]:
    """``(held, principal)``: a held request's answer, or the principal to run as.

    Asked last, after authority and shape, so only a valid request becomes a
    decision a person has to read. Judged on the validated command, never the
    payload: validation coerces (``1`` is ``True``), and a rule read off the
    payload would let a coercible spelling past it. Raises ``PolicyDenied`` on
    a deny rule.
    """

    decision = decide(spec, principal, command, target)
    if decision.rule == Rule.DENY:
        raise PolicyDenied(f"{spec.name} is refused by {decision.source}.")
    if decision.rule == Rule.APPROVE:
        return hold_for_approval(spec, payload, command, target, principal=principal), principal
    if decision.overrides_a_hold:
        # Standing policy is the consent; carried where consent travels.
        return None, replace(principal, approved_by=decision.source)
    return None, principal


def _run(
    spec: CapabilitySpec,
    command: Any,
    *,
    principal: Principal,
    target: str | int | None,
    expected_updated_at: str | None,
    sent: frozenset[str],
) -> dict[str, Any]:
    """Bind the target and run the handler, in one place.

    Every audit event the handler records names the caller and the capability,
    and a command that names a connection attributes them to it too. The
    stale-write token, and the names of the fields the payload held, go only
    to a handler that declares them.
    """

    arguments = _target_keyword(spec, target)
    if takes_stale_write_token(spec.handler):
        arguments[STALE_WRITE_KEYWORD] = expected_updated_at
    if takes_sent_fields(spec.handler):
        arguments[SENT_KEYWORD] = sent
    with (
        audit_operation(operation=spec.name, principal=principal),
        audit_connection(getattr(command, "connection_ref", "") or ""),
    ):
        result = spec.handler(command, principal=principal, **arguments)
    if spec.effect != "read":
        # Every write a person or credential makes passes here, so this is the
        # one place that starts the controller at once rather than at the next
        # minute's ask; whether a pass is due is still `pass_due`'s alone, asked
        # on the doorbell's thread so the request pays for none of it.
        # Robust: a write that committed does not fail for a hint that could
        # not be given.
        transaction.on_commit(ask_soon, robust=True)
    if Capability.GOVERN_AGENTS in spec.required_capabilities:
        # Every brake a person sets on an agent is such a capability, so a
        # stream held back an event while its agent was refused hears of it
        # here, once the change commits, however many brakes there are.
        agent_registry.standing_changed()
    return dict(result)


def execute_approved(
    spec: CapabilitySpec,
    command: Any,
    target: str | int | None,
    *,
    principal: Principal,
    sent: frozenset[str],
) -> dict[str, Any]:
    """Run a capability a person has just agreed to, held payload and all.

    The approval gate is not consulted again: the decision that calls this
    satisfied it. Takes a spec rather than a name, so nothing can route here by
    sending a string, and raises rather than answering in an adapter's error
    shape, so the person's page reports it and the transaction keeps the request.
    """

    return _run(spec, command, principal=principal, target=target, expected_updated_at=None, sent=sent)
