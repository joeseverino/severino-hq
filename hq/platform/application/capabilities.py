"""Deterministic JSON capability registry for every HQ adapter."""

import secrets
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypedDict

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from hq.platform.core.audit import audit_connection, operation_context
lazy from hq.platform.core.doorbell import ask_soon

from .approvals import RefusedHold, hold_for_approval
from .capability_policy import Rule, decide
from .contracts import DOTTED_NAME
from .denials import record_denial
from .idempotency import (
    IdempotencyConflict,
    InvalidIdempotencyKey,
    execute_once,
    request_fingerprint,
    validate_key,
)
from .input_errors import (
    Refusal,
    django_refusal,
    pydantic_refusal,
    unknown_field_errors,
)
from .integration_specs import (
    IDEMPOTENCY_FIELD,
    STALE_WRITE_KEYWORD,
    TARGET_KINDS,
    CapabilitySpec,
    capability_schema,
    declares_idempotency_key,
    takes_stale_write_token,
)
from .integrations import integration_graph
from .labels import human_label
from .projection import QueryRefusal
from .record_errors import HostRefusal
from .security import (
    AuthorizationError,
    Capability,
    PolicyDenied,
    Principal,
    could_hold,
    require_all,
)


class _UnusableTarget(Exception):
    """The target arrived, but not as the kind the capability declared."""


class _UnknownFields(Exception):
    """The payload carries fields the command does not have.

    Its own type rather than a ValueError, which the generic handler would turn
    into "could not be executed": leaving a caller who misspelled a field no
    way to learn which one. It carries Pydantic's ``extra_forbidden`` entries,
    so it is answered exactly as a StrictCommand's own refusal is.
    """

    def __init__(self, fields: list[str]):
        super().__init__()
        self.errors = unknown_field_errors(fields)


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

    name: str
    label: str
    verb: str
    variants: dict[str, str]
    summary: str
    effect: str
    required_capabilities: list[str]
    target: str | None
    target_label: str
    target_help: str
    target_query: dict[str, str | int | float | bool]
    execution_notes: list[str]
    target_initial_fields: list[str]
    resource: str | None
    input_schema: dict[str, Any]


class InvalidCapabilityQuery(QueryRefusal):
    """A narrowing input the capability catalog cannot be read by; ``reason`` says which."""


def _narrowed(name: str, resource: str) -> list[CapabilitySpec]:
    """The capabilities under the dotted ``name`` and about ``resource``, refusing what names nothing.

    ``name`` is a whole capability name or a prefix of whole segments
    (``infrastructure`` holds ``infrastructure.reconcile``), so a filter never
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


def describe_capabilities(*, principal: Principal, name: str = "", resource: str = "") -> dict[str, Any]:
    """Return stable JSON Schemas and operational effects for every capability
    ``principal``'s interface could ever hold, or those under a dotted ``name``
    and about a ``resource``.

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
        }
        for spec in _narrowed(name.strip(), resource.strip())
        if could_hold(principal.interface, spec.required_capabilities)
    ]
    return {"ok": True, "schema_version": 2, "capabilities": described}


def _target_keyword(spec: CapabilitySpec, target: str | int | None) -> dict[str, Any]:
    """Bind the target to the keyword its capability declared it under."""

    if not spec.target_kind:
        return {}
    kind = TARGET_KINDS[spec.target_kind]
    try:
        return {kind.keyword: kind.coerce(target)}
    except (ValueError, TypeError) as exc:
        raise _UnusableTarget from exc


def execute_capability(
    name: str,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Validate JSON and execute one allowlisted application capability."""

    spec = capability_registry().get(name)
    if spec is None:
        return _error("unknown_capability", f"Unknown capability {name!r}.")
    refused = _target_refusal(spec, name, target)
    if refused is not None:
        return refused

    try:
        return _authorized_run(
            spec,
            payload,
            principal=principal,
            target=target,
            expected_updated_at=expected_updated_at,
        )
    except _UnusableTarget:
        return _error("invalid_input", f"{name} requires a {spec.target_kind} target.")
    except _UnknownFields as exc:
        return _invalid(pydantic_refusal(name, exc.errors))
    except RefusedHold as exc:
        # Said in full, unlike the generic failure below. The remedy is neither
        # a retry nor a fix to the request (somebody has to answer what is
        # already waiting, or what the call is about has to be readable) and a
        # caller cannot work that out from "could not be executed".
        record_denial(
            interface=principal.interface,
            actor=principal.actor,
            capability=name,
            reason=exc.code,
        )
        return _error(exc.code, exc.reason)
    except AuthorizationError as exc:
        # The one refusal point for every adapter.
        record_denial(
            interface=principal.interface,
            actor=principal.actor,
            capability=name,
            reason=exc.code,
        )
        return _error(exc.code, exc.reason)
    except (DjangoValidationError, HostRefusal, TypeError, ValueError) as exc:
        return _domain_refusal(name, exc)


def _domain_refusal(name: str, exc: Exception) -> dict[str, Any]:
    """What the command's own layer said no with, answered as a code."""

    if isinstance(exc, PydanticValidationError):
        return _invalid(pydantic_refusal(name, exc.errors()))
    if isinstance(exc, DjangoValidationError):
        return _invalid(django_refusal(name, exc))
    if isinstance(exc, HostRefusal):
        # Host-written text naming no request value (record_errors.py), and
        # what tells a caller what it has to change.
        return _error(exc.code, exc.reason)
    # Neither handler nor dependency exception text crosses an adapter.
    # It can contain argument names, provider responses, paths, or values
    # from the request. The capability name is registry-owned and safe.
    return _error("operation_failed", f"{name} could not be executed.")


def _authorized_run(
    spec: CapabilitySpec,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None,
    expected_updated_at: str | None,
) -> dict[str, Any]:
    """Check authority, shape and consent, then run once under the caller's key."""

    # Authority first, then the payload, then the target. A caller who may
    # not run this at all is told exactly that, and learns nothing about
    # what shape of target it would have taken.
    authorize_capability(spec, principal)
    _refuse_unknown_fields(spec, payload)
    key, payload = _retry_key(spec, payload)
    command = validate_command(spec, payload)
    # A held request has acted on nothing, so it is answered afresh each
    # time: the same key runs the command once a person has agreed.
    held, acting = _consent(spec, payload, command, target, principal)
    if held is not None:
        return held

    def act() -> dict[str, Any]:
        return _run(
            spec,
            command,
            principal=acting,
            target=target,
            expected_updated_at=expected_updated_at,
        )

    if not key:
        return act()
    try:
        return _once(
            spec,
            key,
            {"command": payload, "target": target, "expected_updated_at": expected_updated_at},
            act,
            actor=principal.actor,
        )
    except IdempotencyConflict:
        return _error(
            "idempotency_conflict",
            f"This {IDEMPOTENCY_FIELD} was already used for a different request.",
        )


def _retry_key(spec: CapabilitySpec, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(key, payload)``: the caller's retry key, and the payload its command reads.

    The key is optional on every capability that takes one. A command type that
    declares the field stores it with the operation it queues, so it receives
    the caller's key, or a fresh one when the caller sent none. Any other
    command never sees the field.
    """

    if spec.effect == "read" or not isinstance(payload, dict):
        return "", payload
    sent = payload.get(IDEMPOTENCY_FIELD)
    try:
        key = "" if sent is None else validate_key(sent)
    except (InvalidIdempotencyKey, TypeError) as exc:
        # Answered as any other field of the wrong shape is.
        raise DjangoValidationError({IDEMPOTENCY_FIELD: "Must be 1-128 URL-safe characters."}) from exc
    rest = {name: value for name, value in payload.items() if name != IDEMPOTENCY_FIELD}
    if declares_idempotency_key(spec.command_type):
        rest[IDEMPOTENCY_FIELD] = key or f"command:{secrets.token_urlsafe(18)}"
    return key, rest


def _once(
    spec: CapabilitySpec,
    key: str,
    request: dict[str, Any],
    act: Callable[[], dict[str, Any]],
    *,
    actor: str,
) -> dict[str, Any]:
    """Run ``act`` under the caller's key, or return what it returned the first time.

    The key belongs to the actor and to one request: the same key with another
    capability, target or payload is a conflict. A call that raises, or answers
    with a refusal of its own, keeps nothing, so its key is free for the
    corrected request.
    """

    result, _status, _replayed = execute_once(
        actor=actor,
        key=key,
        request_sha256=request_fingerprint(spec.name, request, api_version=2),
        operation=lambda: (act(), 200),
        scope="command",
        keep=lambda answer, _status: answer.get("ok", False),
    )
    return result


def _target_refusal(spec: CapabilitySpec, name: str, target: str | int | None) -> dict[str, Any] | None:
    """The error for a target the capability requires and lacks, or refuses."""

    if spec.target_kind and target is None:
        return _error("target_required", f"{name} requires a target.")
    if not spec.target_kind and target is not None:
        return _error("target_not_allowed", f"{name} does not accept a target.")
    return None


def validate_command(spec: CapabilitySpec, payload: dict[str, Any]) -> Any:
    """The command ``payload`` becomes: what runs, and so what policy judges."""

    return TypeAdapter(spec.command_type).validate_python(payload)


def _consent(
    spec: CapabilitySpec,
    payload: dict[str, Any],
    command: Any,
    target: str | int | None,
    principal: Principal,
) -> tuple[dict[str, Any] | None, Principal]:
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
) -> dict[str, Any]:
    """Bind the target and run the handler, in one place.

    Every audit event the handler records names the caller and the capability,
    and a command that names a connection attributes them to it too. The
    stale-write token goes only to a handler that declares it.
    """

    arguments = _target_keyword(spec, target)
    if takes_stale_write_token(spec.handler):
        arguments[STALE_WRITE_KEYWORD] = expected_updated_at
    with (
        operation_context(interface=principal.interface, actor=principal.actor, operation=spec.name),
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
    return result


def execute_approved(
    spec: CapabilitySpec,
    command: Any,
    target: str | int | None,
    *,
    principal: Principal,
) -> dict[str, Any]:
    """Run a capability a person has just agreed to, held payload and all.

    The approval gate is not consulted again: the decision that calls this
    satisfied it. Takes a spec rather than a name, so nothing can route here by
    sending a string, and raises rather than answering in an adapter's error
    shape, so the person's page reports it and the transaction keeps the request.
    """

    return _run(spec, command, principal=principal, target=target, expected_updated_at=None)


def _refuse_unknown_fields(spec: CapabilitySpec, payload: dict[str, Any]) -> None:
    """A field the command does not have is an error, not a no-op.

    A misspelled or retired field would otherwise return success for work the
    caller did not ask for.
    """

    # CapabilitySpec accepts host dataclasses and plugin StrictCommand models.
    # Their JSON Schema is already the shared adapter contract, so it is also
    # the single source of field names.
    known = set(capability_schema(spec).get("properties", {}))
    unknown = sorted(set(payload) - known)
    if unknown:
        raise _UnknownFields(unknown)


def _invalid(refusal: Refusal) -> dict[str, Any]:
    """Every validation refusal: which field and why, never the value sent."""

    return _error("invalid_input", refusal.message, refusal.details)


def _error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}
