"""Deterministic JSON capability registry for every HQ adapter."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, time
from decimal import Decimal
from functools import cache
from typing import Annotated, Any, Literal, NamedTuple, TypeAliasType, TypedDict, final, get_type_hints
from uuid import UUID

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError as JSONSchemaError
from pydantic import ConfigDict, TypeAdapter, ValidationError as PydanticValidationError, with_config

from hq.platform.core.audit import audit_connection, audit_operation, record_event, tallied
from hq.platform.core.models import AuditLog
from hq.platform.core.tracing import note, span
lazy from hq.domains.control_plane.providers import PROVIDERS

from .answer_fields import Permitted, Pinned, RequiredPermissions, SchemaVersion
from .approvals import Approved, approval, hold_for_approval
from .call_envelope import read_call
from .capability_policy import Rule, decide
from .contracts import DOTTED_NAME, Effect
from .default_hold import kind_waits
from .describe import Describe, listed
from .held_answer import HeldCall
from .idempotency import (
    KEY_SHAPE,
    IdempotencyConflict,
    InvalidIdempotencyKey,
    answered,
    execute_once,
    request_fingerprint,
    validate_key,
)
from .integration_specs import (
    CONSENT_KEYWORD,
    IDEMPOTENCY_FIELD,
    IDEMPOTENCY_HEADER,
    SENT_KEYWORD,
    STALE_WRITE_KEYWORD,
    TARGET_KINDS,
    CapabilitySpec,
    capability_schema,
    command_adapter,
    command_schema,
    kept_by,
    takes_consent,
    takes_sent_fields,
    takes_stale_write_token,
)
from .compiled_graph import integration_graph
from .integrations import held_graph
from .labels import human_label
from .projection import QueryRefusal
from .record_errors import PolicyError
from .refusal_codes import Code
from .refusal_status import Refusal, next_call, refusal_of, refuse, refused
from .sealing import Sealed, sealer
from .security import (
    CLI_INTERFACE,
    Capability,
    PolicyDenied,
    Principal,
    agent_subject,
    could_ever_hold,
    is_interactive,
)
lazy from . import agent_registry, automation_runs
lazy from .cadence import ringing


def capability_title(name: str) -> str:
    """What a person calls the command named ``name``, registered or not."""

    spec = capability_spec(name)
    return spec.title if spec else human_label(name)


def capability_words(name: str, variant: str = "") -> str:
    """What a button for the command named ``name`` says (``CapabilitySpec.words``)."""

    return command_spec(name).words(variant)


def capability_spec(name: str) -> CapabilitySpec | None:
    """The command named ``name``, or None for a name nothing registers. Only
    an adopted procedure's reads the adopted catalog; every other command is
    the same at every adoption, so a page that names one asks nothing."""

    graph = integration_graph() if automation_runs.runs_procedure(name) else held_graph()
    return graph.capabilities.get(name)


def command_spec(name: str) -> CapabilitySpec:
    """The registered command named ``name`` (``capability_spec``), which a
    caller names knowing it is registered."""

    spec = capability_spec(name)
    if spec is None:
        raise KeyError(name)
    return spec


def capability_registry() -> tuple[CapabilitySpec, ...]:
    """Every command, at the catalog adopted now. It is walked, never
    searched: one command is ``capability_spec``'s."""

    return tuple(integration_graph().capabilities.values())


class CapabilitySummary(TypedDict):
    """One command as an index names it: what it does, without its schema."""

    name: Annotated[str, Describe("The command's dotted name, which you run it by.", examples=("asset.update",))]
    label: Annotated[str, Describe("The command's name, as HQ's pages show it.")]
    summary: Annotated[str, Describe("What the command does, in a sentence.")]
    effect: Effect
    resource: Annotated[
        str | None,
        Describe("The list its targets are read from, by the name `describe_resources` gives it; null for none."),
    ]


class CapabilityDescription(CapabilitySummary):
    """One registry entry as every adapter describes it."""

    permitted: Permitted
    verb: Annotated[str, Describe("What its button says.")]
    variants: Annotated[
        dict[str, str],
        Describe("What its button says in each mode, by mode name, where one label cannot fit every mode."),
    ]
    required_capabilities: RequiredPermissions
    target: Annotated[
        str | None,
        Describe(f"What names the record it acts on: {listed(TARGET_KINDS)}; null for a command that takes none."),
    ]
    target_label: Annotated[str, Describe("The type of record it acts on, as HQ's pages name it; empty for none.")]
    target_help: Annotated[str, Describe("What to send as the target, in a sentence; empty for none.")]
    target_query: Annotated[
        dict[str, str | int | float | bool],
        Describe("What a record must hold to be offered as its target, value by field; empty for any record."),
    ]
    execution_notes: Annotated[list[str], Describe("What running it does, step by step, a sentence each.")]
    target_initial_fields: Annotated[
        list[str], Describe("The command's fields filled in from the target's record, which you may change.")
    ]
    input_schema: Annotated[dict[str, Any], Describe("The JSON Schema of the command it takes as `payload`.")]


class InvalidCapabilityQuery(QueryRefusal):
    """A narrowing input the capability catalog cannot be read by; ``reason`` says which."""


def _reachable(principal: Principal, *, name: str = "", resource: str = "") -> list[CapabilitySpec]:
    """What ``principal``'s interface could ever hold under the dotted ``name`` and about ``resource``.

    ``name`` is a whole capability name or a prefix of whole segments (``infrastructure`` holds
    ``infrastructure.resource.reconcile``), so a filter never matches half a word; one that names nothing is refused.
    """

    name, resource = name.strip(), resource.strip()
    if name and not DOTTED_NAME.match(name):
        raise InvalidCapabilityQuery("capabilities: name is a dotted capability name or a prefix of one.")
    if resource and resource not in held_graph().resources:
        raise InvalidCapabilityQuery("HQ has no list by that name. describe_resources names each one.")
    return [
        spec
        for spec in capability_registry()
        if (not name or spec.name == name or spec.name.startswith(f"{name}."))
        and (not resource or spec.subject_resource == resource)
        and could_ever_hold(principal, spec.required_capabilities)
    ]


# The shape describe_capabilities answers in; it moves when that shape does.
CAPABILITIES_SCHEMA_VERSION = 2
# That version as every door declares it: the one number the catalog carries.
CapabilitiesVersion = Annotated[SchemaVersion, Pinned(CAPABILITIES_SCHEMA_VERSION)]


class CapabilityCatalog(TypedDict):
    """What ``describe_capabilities`` answers."""

    schema_version: CapabilitiesVersion
    capabilities: Annotated[list[CapabilityDescription], Describe("Each command asked for that you can run from here.")]


def capability_index(*, principal: Principal) -> list[tuple[CapabilitySummary, bool]]:
    """Each capability ``principal``'s interface could ever hold, as an index names it, and whether it is permitted."""

    return [(_summarized(spec), spec.permitted_to(principal)) for spec in _reachable(principal)]


def _summarized(spec: CapabilitySpec) -> CapabilitySummary:
    return {
        "name": spec.name,
        "label": spec.title,
        "summary": spec.summary,
        "effect": spec.effect,
        "resource": spec.subject_resource,
    }


def describe_capabilities(*, principal: Principal, name: str = "", resource: str = "") -> CapabilityCatalog:
    """Every capability ``principal``'s interface could ever hold, or those under a dotted ``name`` and about a
    ``resource``, with its JSON Schema and effects and whether ``principal`` is ``permitted`` it; a token is never
    shown a command only a person can run, whatever its grant.
    """

    described: list[CapabilityDescription] = [
        {
            **_summarized(spec),
            "verb": spec.words(),
            "variants": dict(spec.variants),
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
            "input_schema": capability_schema(spec),
            "permitted": spec.permitted_to(principal),
        }
        for spec in _reachable(principal, name=name, resource=resource)
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


def first_answer(principal: Principal, key: str) -> dict[str, Any] | None:
    """What ``principal``'s call under retry ``key`` was answered when it ran, or None while it has none."""

    return answered(actor=agent_subject(principal), key=key)


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
    key_in_payload: bool = True,
) -> Ran:
    """Run one capability: the one place a retry key is read, required and kept.

    ``execute_capability`` is this with the answer alone, the signature
    ``hq_sdk`` publishes. A door with no place beside the payload sends the key
    as its ``idempotency_key``; the API's header says ``key_in_payload=False``.
    """

    with span(f"capability {name}", **{"hq.capability": name, "hq.interface": principal.interface}):
        ran = _named_run(
            name,
            payload,
            principal=principal,
            target=target,
            expected_updated_at=expected_updated_at,
            idempotency_key=idempotency_key,
            key_in_payload=key_in_payload,
        )
        failed = refused(ran.answer)
        note(
            **{"hq.ok": not failed, "hq.replayed": ran.replayed},
            **({"error.type": refusal_of(ran.answer).code} if failed else {}),
        )
        return ran


def _named_run(
    name: str,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None,
    expected_updated_at: str | None,
    idempotency_key: str | None,
    key_in_payload: bool,
) -> Ran:
    spec = capability_spec(name)
    if spec is None:
        return Ran(
            refuse(
                Code.UNKNOWN_CAPABILITY,
                f"Unknown capability {name!r}. describe_capabilities lists the ones you may run.",
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
            key_in_payload=key_in_payload,
        )
    except InvalidIdempotencyKey as exc:
        return Ran(refuse(Code.INVALID_IDEMPOTENCY_KEY, exc.reason))
    except Exception as exc:  # noqa: BLE001 - every other failure is answered in the one refusal shape
        return Ran(adapter_refusal(name, exc, principal=principal))


def adapter_refusal(name: str, exc: Exception, *, principal: Principal | None) -> dict[str, Any]:
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
    key_in_payload: bool,
) -> Ran:
    """Check authority, the target, the retry key, shape and consent, then run once under the caller's key."""

    # Authority first, then the target, the key and the payload. A caller who
    # may not run this at all is told exactly that, and learns nothing about
    # what target it would have taken.
    spec.authorize(principal)
    refused = _target_refusal(spec, target, expected_updated_at)
    if refused is not None:
        return Ran(refused)
    key, payload = _retry_key(spec, payload, sent_key, key_in_payload=key_in_payload)
    if not key and key_required(spec.effect, principal):
        # A retry of a change sent without a key could act twice.
        where = f"payload.{IDEMPOTENCY_FIELD}" if key_in_payload else f"{IDEMPOTENCY_HEADER} header"
        return Ran(
            refuse(
                Code.IDEMPOTENCY_KEY_REQUIRED,
                f"A capability that changes something needs an idempotency key. Send a new {where}, "
                "and the same one when you retry this change.",
                {"field": where.removesuffix(" header")},
            )
        )
    command = validate_command(spec, payload, target)
    # A held request has acted on nothing, so it is answered afresh each
    # time: the same key runs the command once a person has agreed.
    consented = _consent(spec, payload, command, target, principal)
    if not isinstance(consented, Consented):
        return Ran({**consented})

    def act() -> dict[str, Any]:
        return _run(
            spec,
            command,
            consented=consented,
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
            actor=agent_subject(principal),
        )
    except IdempotencyConflict as exc:
        return Ran(refuse(Code.IDEMPOTENCY_CONFLICT, exc.reason))


def key_required(effect: Effect, principal: Principal) -> bool:
    """Whether ``principal`` must send a retry key to run a capability of ``effect``: a change, from any door but the shell's.

    A token's client retries what a lost connection left unanswered, and a
    browser resends a form (``web_writes``). An allowlist of the one door not
    asked, so a door added later is asked, as ``INTERACTIVE_INTERFACES`` is
    built the safe way round.
    """

    return effect.changes and principal.interface not in KEYLESS_INTERFACES


# Not asked for a key: the host's shell, where a person runs a command once, by hand.
KEYLESS_INTERFACES = frozenset({CLI_INTERFACE})


def _retry_key(
    spec: CapabilitySpec, payload: dict[str, Any], sent: str | None, *, key_in_payload: bool
) -> tuple[str, dict[str, Any]]:
    """``(key, payload)``: the caller's retry key, and the payload its command reads.

    A call's key arrives one way: beside the payload (``sent``), or, where none
    is and the door takes one there, in it. A key in the payload otherwise, or
    of a read, is a field the command does not have. No command sees the key:
    a retry is this runner's to answer.
    """

    if not spec.effect.changes or not isinstance(payload, dict):
        return "", payload
    if sent is not None or not key_in_payload or IDEMPOTENCY_FIELD not in payload:
        return ("" if sent is None else _valid_key(sent)), payload
    # A payload's key is judged whenever the field is there: a null is a key
    # of the wrong shape, as the document declares it, never a key left out.
    key = _valid_key(payload[IDEMPOTENCY_FIELD])
    return key, {name: value for name, value in payload.items() if name != IDEMPOTENCY_FIELD}


def _valid_key(given: object) -> str:
    """``given`` as a retry key; raises ``InvalidIdempotencyKey`` for any other value.

    A key sent empty is a key of the wrong shape, as the API document declares
    it, never a key left out: a door whose form posts the field empty leaves it
    out itself (``web_writes._call_key``).
    """

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
            Code.TARGET_REQUIRED,
            f"{spec.name} requires a target: send the record it acts on as target.",
            {"field": "target"},
        )
    if not spec.target_kind and target is not None:
        return refuse(
            Code.TARGET_NOT_ALLOWED,
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

    sent = payload
    kept = kept_by(spec) if target is not None else None
    if kept is not None:
        (current,) = _target_keyword(spec, target).values()
        payload = {**kept.read(current), **payload}
    mistyped = _mistyped(spec.command_type, sent)
    if mistyped:
        raise PydanticValidationError.from_exception_data(spec.name, mistyped)
    return command_adapter(spec.command_type).validate_python(payload)


# What Pydantic calls a value of the wrong JSON type, by the type the schema
# gives; the first a union lists names the refusal.
_TYPE_ERRORS = {
    "array": "list_type",
    "boolean": "bool_type",
    "integer": "int_type",
    "null": "none_required",
    "number": "float_type",
    "object": "dict_type",
    "string": "string_type",
}
# A value a door hands HQ already parsed: the text the schema says, as Python holds it.
_JSON_TYPES = Draft202012Validator.TYPE_CHECKER
_SENT_TYPES = _JSON_TYPES.redefine_many(
    {
        "number": lambda checker, value: _JSON_TYPES.is_type(value, "number") or isinstance(value, Decimal),
        "string": lambda checker, value: isinstance(value, str | date | time | UUID),
    }
)
_Sent = validators.extend(Draft202012Validator, type_checker=_SENT_TYPES)


@cache
def _published(command_type: type) -> Any:
    return _Sent(command_schema(command_type))


def _mistyped(command_type: type, payload: Any) -> list[Any]:
    """Each value ``payload`` sends in a JSON type its published schema does not give it.

    Pydantic's lax mode, which reads a form's text, takes ``0`` for false and
    ``"3"`` for 3; a door's payload is held to the document's types first, so
    what the document refuses is refused. The rest Pydantic judges.
    """

    found = []
    for error in _published(command_type).iter_errors(payload):
        wrong = _wrong_type(error)
        if wrong is None:
            continue
        wanted, held = wrong
        first = wanted[0] if isinstance(wanted, list) else wanted
        found.append({"type": _TYPE_ERRORS[first], "loc": tuple(held.absolute_path), "input": held.instance})
    return found


def as_documented(command_type: type, payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` with each field the document's types refuse read as the command's own type reads it.

    A form posts text: a weekday's checkbox sends ``"0"`` where the document
    says an integer. The web door reads its form through this before the call,
    so what it sends, holds for approval and records is the JSON an agent
    would send. A field its type does not read stays as sent, for the call's
    refusal to name.
    """

    wrong = {error["loc"][0] for error in _mistyped(command_type, payload) if error["loc"]}
    return {
        name: _read_as_typed(command_type, name, value) if name in wrong else value for name, value in payload.items()
    }


def _read_as_typed(command_type: type, name: str, value: Any) -> Any:
    adapter = _field_adapter(command_type, name)
    if adapter is None:
        return value
    try:
        return adapter.dump_python(adapter.validate_python(value), mode="json")
    except PydanticValidationError:
        return value


@cache
def _field_adapter(command_type: type, name: str) -> TypeAdapter[Any] | None:
    hint = get_type_hints(command_type, include_extras=True).get(name)
    if hint is None:
        return None
    # Closed as the command's own validator is (``command_adapter``), so a
    # field it would refuse is never read into one it takes.
    alias: Any = TypeAliasType(f"{command_type.__name__}_{name}", hint)
    return TypeAdapter(alias, config=ConfigDict(extra="forbid"))


def _wrong_type(error: JSONSchemaError) -> tuple[Any, JSONSchemaError] | None:
    """The JSON type ``error`` says a value should have been sent in, and the
    error that holds the value; ``None`` for an error of another kind.
    """

    if error.validator == "type":
        return error.validator_value, error
    if error.validator not in {"anyOf", "oneOf"} or not error.context:
        return None
    if all(branch.validator == "type" and not branch.relative_path for branch in error.context):
        return error.context[0].validator_value, error
    by_branch: dict[Any, list[tuple[Any, JSONSchemaError]]] = {}
    for found in error.context:
        wrong = _wrong_type(found)
        by_branch.setdefault(found.relative_schema_path[0], []).extend(() if wrong is None else (wrong,))
    if not all(by_branch.values()):
        return None
    return max(
        (wrong for found in by_branch.values() for wrong in found), key=lambda wrong: len(wrong[1].absolute_path)
    )


# What a declaration write the consent does not admit is refused with.
NEEDS_APPROVAL = "This change needs approval. Ask for it through the API or MCP, then approve it in HQ in the browser."


@final
class Consented(Sealed):
    """What the capability runner decided for one run, which every declaration write asks (``sealing``).

    A declaration writer takes this in place of a principal and asks it for the
    kind it writes (``Consent.require``), so no gated declaration is written
    without a person's consent, whichever handler writes it. Only ``_consent``
    and ``execute_approved`` make one, and who acts and who approved are read
    from it alone.
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Consent:
    """What a ``Consented`` carries: who acts, and whether a person approved the run."""

    principal: Principal
    approved: bool = False

    def admits(self, kind: str) -> bool:
        """Whether this run may write a declaration of ``kind``.

        The one place the rule lives. The hold judges a run by the kind its
        command names; this judges the kind actually written, so a write of a
        kind the command never named is refused all the same.
        """

        provider = PROVIDERS.get(kind)
        if provider is None or not kind_waits(provider, self.principal):
            return True
        return self.approved or is_interactive(self.principal) or bool(self.principal.approved_by)

    def require(self, kind: str) -> None:
        """Refuse a write of ``kind`` this run may not make (``admits``)."""

        if not self.admits(kind):
            raise PolicyError(NEEDS_APPROVAL)


_seal, consent = sealer(Consented, Consent)


def _consent(
    spec: CapabilitySpec,
    payload: dict[str, Any],
    command: Any,
    target: str | int | None,
    principal: Principal,
) -> HeldCall | Consented:
    """A held request's answer, or the consent to run as.

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
        held = hold_for_approval(spec, payload, command, target, principal=principal)
        if held is not None:
            return held
    if decision.overrides_a_hold:
        # Standing policy is the consent; carried where consent travels.
        return _seal(Consent(replace(principal, approved_by=decision.source)))
    return _seal(Consent(principal))


# What the act of a write that recorded no row of its own is filed under.
CAPABILITY_AUDIT_TYPE = "Capability"


def _run(
    spec: CapabilitySpec,
    command: Any,
    *,
    consented: Consented,
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

    principal = consent(consented).principal
    arguments = _target_keyword(spec, target)
    # A handler that writes a declaration takes the consent, and is handed no
    # principal beside it.
    if takes_consent(spec.handler):
        arguments[CONSENT_KEYWORD] = consented
    else:
        arguments["principal"] = principal
    if takes_stale_write_token(spec.handler):
        arguments[STALE_WRITE_KEYWORD] = expected_updated_at
    if takes_sent_fields(spec.handler):
        arguments[SENT_KEYWORD] = sent
    with (
        audit_operation(operation=spec.name, principal=principal),
        audit_connection(getattr(command, "connection_ref", "") or ""),
        tallied() as written,
        ringing() as asked,
    ):
        result = spec.handler(command, **arguments)
        if spec.effect.changes and not written:
            # A write whose handler recorded no row still leaves its act: what
            # ran, under the row's operation, and how it ended.
            record_event(
                action=AuditLog.Action.DONE if result.get("ok", True) else AuditLog.Action.FAILED,
                type_label=CAPABILITY_AUDIT_TYPE,
                message=str(result.get("message") or ""),
                required=True,
            )
        if spec.effect.changes:
            # Every write a person or credential makes passes here, so this is
            # the one place that starts the controller at once rather than at
            # the next minute's ask; whether a pass is due is still
            # `pass_due`'s alone, asked on the doorbell's thread so the request
            # pays for none of it.
            asked.add("controller")
    if Capability.GOVERN_AGENTS in spec.required_capabilities:
        # Every brake a person sets on an agent is such a capability, so a
        # stream held back an event while its agent was refused hears of it
        # here, once the change commits, however many brakes there are.
        agent_registry.standing_changed()
    return dict(result)


def execute_approved(approved: Approved) -> dict[str, Any]:
    """Run the change a person has just approved, held payload and all.

    The approval gate is not consulted again: the ``Approved`` only a
    person's approval makes is its proof, and ``approval`` refuses anything
    else. Raises rather than answering in an adapter's error shape, so the
    person's page reports it and the transaction keeps the request.
    """

    change = approval(approved)
    consented = _seal(Consent(change.acting, approved=True))
    return _run(
        change.spec,
        change.command,
        consented=consented,
        target=change.target,
        expected_updated_at=None,
        sent=change.sent,
    )
