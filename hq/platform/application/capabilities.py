"""Deterministic JSON capability registry for every HQ adapter."""

import secrets
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypedDict

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from hq.platform.core.audit import audit_connection

from .approvals import RefusedHold, hold_for_approval
from .capability_policy import Rule, decide
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
    TARGET_KINDS,
    CapabilitySpec,
    capability_schema,
    declares_idempotency_key,
)
from .integrations import integration_graph
from .labels import human_label
from .security import (
    AuthorizationError,
    Capability,
    PolicyDenied,
    Principal,
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


def capability_registry() -> dict[str, CapabilitySpec]:
    return dict(integration_graph().capabilities)


def authorize_capability(spec: CapabilitySpec, principal: Principal) -> None:
    """Apply the registry's one authorization rule for every adapter."""

    require_all(principal, spec.required_capabilities)


class CapabilityDescription(TypedDict):
    """One registry entry as every adapter describes it."""

    name: str
    label: str
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


def describe_capabilities() -> dict[str, Any]:
    """Return stable JSON Schemas and operational effects for every capability."""

    described: list[CapabilityDescription] = [
        {
            "name": spec.name,
            "label": spec.title,
            "summary": spec.summary,
            "effect": spec.effect,
            "required_capabilities": [
                capability.value
                if isinstance(capability, Capability)
                else capability
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
        for spec in integration_graph().capabilities.values()
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
    except PydanticValidationError as exc:
        return _invalid(pydantic_refusal(name, exc.errors()))
    except DjangoValidationError as exc:
        return _invalid(django_refusal(name, exc))
    except (TypeError, ValueError):
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
    command: Any = TypeAdapter(spec.command_type).validate_python(payload)
    # A held request has acted on nothing, so it is answered afresh each
    # time: the same key runs the command once a person has agreed.
    held, acting = _consent(spec, spec.name, payload, target, principal)
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
        raise DjangoValidationError(
            {IDEMPOTENCY_FIELD: "Must be 1-128 URL-safe characters."}
        ) from exc
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
    capability, target or payload is a conflict. A call that raises commits
    nothing, so its key is free for the corrected request.
    """

    result, _status, _replayed = execute_once(
        actor=actor,
        key=key,
        request_sha256=request_fingerprint(spec.name, request, api_version=2),
        operation=lambda: (act(), 200),
        scope="command",
    )
    return result


def _target_refusal(
    spec: CapabilitySpec, name: str, target: str | int | None
) -> dict[str, Any] | None:
    """The error for a target the capability requires and lacks, or refuses."""

    if spec.target_kind and target is None:
        return _error("target_required", f"{name} requires a target.")
    if not spec.target_kind and target is not None:
        return _error("target_not_allowed", f"{name} does not accept a target.")
    return None


def _consent(
    spec: CapabilitySpec,
    name: str,
    payload: dict[str, Any],
    target: str | int | None,
    principal: Principal,
) -> tuple[dict[str, Any] | None, Principal]:
    """``(held, principal)``: a held request's answer, or the principal to run as.

    Asked last, after authority and shape, so only a valid request becomes a
    decision a person has to read. Raises ``PolicyDenied`` on a deny rule.
    """

    decision = decide(spec, principal, payload, target)
    if decision.rule == Rule.DENY:
        raise PolicyDenied(f"{name} is refused by {decision.source}.")
    if decision.rule == Rule.APPROVE:
        return hold_for_approval(spec, payload, target, principal=principal), principal
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
    """Bind the target and run the handler. One line, and one place.

    A command that names a connection attributes its audit events to it.
    """

    with audit_connection(getattr(command, "connection_ref", "") or ""):
        return spec.handler(
            command,
            principal=principal,
            expected_updated_at=expected_updated_at,
            **_target_keyword(spec, target),
        )


def execute_approved(
    spec: CapabilitySpec,
    payload: dict[str, Any],
    target: str | int | None,
    *,
    principal: Principal,
) -> dict[str, Any]:
    """Run a capability a person has just agreed to, held payload and all.

    The gate is deliberately not consulted again. It has already been satisfied,
    by the decision that called this, and asking it a second time would hold the
    approval for an approval. Reachable only from that decision, which is why it
    takes a spec rather than a name: nothing can route to it by sending a string.

    Errors are raised rather than projected into an adapter's error shape. The
    caller here is the page a person is standing on, and it reports what went
    wrong on that page while leaving the request as it was, which is what the
    surrounding transaction guarantees.
    """

    command: Any = TypeAdapter(spec.command_type).validate_python(payload)
    return _run(
        spec, command, principal=principal, target=target, expected_updated_at=None
    )


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
