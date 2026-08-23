"""Versioned machine-client transport over the shared capability registry.

This adapter adds no capability, no domain model, and no business rule. Every
command it runs is already in HQ's registry, which is what keeps the web UI,
the CLI, MCP and a Shortcut from drifting into four behaviours. A phone cannot
develop its own idea of what a domain record is.

The version is in the path, not a header: a Shortcut on a phone you have not
opened in six months is a normal state, and a URL that quietly changed meaning
is the failure this prevents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from application.capabilities import (
    authorize_capability,
    capability_registry,
    describe_capabilities,
    execute_capability,
)
from application.security import AuthorizationError
from application.resources import (
    InvalidResourceInput,
    ResourceNotFound,
    UnknownResource,
    UnsupportedResourceOperation,
    describe_resources,
    get_resource as get_application_resource,
    list_resource as list_application_resource,
)

from .idempotency import (
    IdempotencyConflict,
    InvalidIdempotencyKey,
    execute_once,
    request_fingerprint,
    validate_key,
)
from .security import TokenError, api_principal, granted, is_configured, verify

CURRENT_API_VERSION = 2

REALM = 'Bearer realm="Severino HQ"'

# HQ answers a failed capability with its own error code. Mapping the ones that
# mean something other than "you sent nonsense" keeps a client on HTTP status
# alone for control flow, which is all a Shortcut can branch on comfortably.
CAPABILITY_STATUS = {
    "unknown_capability": 404,
    "forbidden": 403,
    "operation_failed": 409,
}


def _reject_json_constant(value: str):
    raise ValueError(f"{value} is not valid JSON")


def _strict_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON field {key!r}")
        value[key] = item
    return value


def _json(payload: dict[str, Any], *, status: int = 200) -> HttpResponse:
    response = HttpResponse(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        content_type="application/json",
        status=status,
    )
    # Operational data reached with a bearer credential: correct nowhere but
    # the client that asked for it.
    response["Cache-Control"] = "private, no-store"
    return response


def _ok(data: Any, *, status: int = 200) -> HttpResponse:
    return _json({"ok": True, "data": data}, status=status)


def _fail(message: str, *, code: str, status: int, details: Any = None) -> HttpResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    response = _json({"ok": False, "error": error}, status=status)
    if status == 401:
        # A native client cannot use an HTML login page. Saying *how* to
        # authenticate is the difference between a retryable failure and a
        # Shortcut that silently shows a wall of markup.
        response["WWW-Authenticate"] = REALM
    return response


def _principal(request):
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise TokenError("An access token is required.")
    claims = verify(value.strip())
    return api_principal(claims), claims


def _request_schema(spec: dict[str, Any]) -> dict[str, Any]:
    # Pydantic emits local refs such as ``#/$defs/Record``. Once the command
    # schema is nested under ``payload`` those refs still resolve from the
    # document root, so hoist its definitions into the envelope root instead
    # of publishing a schema that only looks valid for flat commands.
    input_schema = {
        key: value for key, value in spec["input_schema"].items() if key != "$defs"
    }
    properties: dict[str, Any] = {
        "payload": input_schema,
        "expected_updated_at": {"type": "string", "format": "date-time"},
    }
    required = []
    target = spec.get("target")
    if target:
        properties["target"] = (
            {
                "oneOf": [
                    {"type": "integer"},
                    {"type": "string", "pattern": r"^-?[0-9]+$"},
                ]
            }
            if target == "integer"
            else {"type": "string", "minLength": 1}
        )
        required.append("target")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
    if definitions := spec["input_schema"].get("$defs"):
        schema["$defs"] = definitions
    return schema


def _endpoint(methods: tuple[str, ...]):
    """Authenticate, then put every failure in the same envelope.

    CSRF-exempt by construction rather than by concession: these views read the
    Authorization header and never the session cookie, so a browser cannot make
    an authenticated request to them at all.
    """

    def decorate(view):
        @csrf_exempt
        def wrapper(request, *args, **kwargs):
            if not is_configured():
                return _fail(
                    "The machine API is not configured on this deployment.",
                    code="not_configured",
                    status=503,
                )
            if request.method not in methods:
                response = _fail(
                    f"{request.method} is not allowed here.",
                    code="method_not_allowed",
                    status=405,
                )
                response["Allow"] = ", ".join(methods)
                return response
            try:
                principal, claims = _principal(request)
            except TokenError as exc:
                return _fail(exc.reason, code=exc.code, status=401)
            except AuthorizationError as exc:
                return _fail(exc.reason, code=exc.code, status=403)
            request.principal = principal
            request.token_claims = claims
            response = view(request, *args, **kwargs)
            if kwargs.get("version") == 1:
                response["Deprecation"] = "true"
                response["Link"] = '</api/v2/>; rel="successor-version"'
            return response

        wrapper.__name__ = view.__name__
        wrapper.__doc__ = view.__doc__
        # A fact a test can read, rather than one a reviewer has to notice.
        # `/api/` is exempt from the login *redirect*, so a view added here
        # without this decorator is not merely unprotected by convention -- it
        # is served to anyone who asks. `core.test_security` walks these routes
        # and fails if one lacks the mark.
        wrapper.__hq_authenticated__ = True
        return wrapper

    return decorate


@_endpoint(("GET",))
def root(request, version: int):
    """What this is, and what the presented credential may actually do."""

    links = {"capabilities": f"/api/v{version}/capabilities/"}
    if version >= 2:
        links["resources"] = f"/api/v{version}/resources/"
    return _ok(
        {
            "service": "severino-hq",
            "api_version": version,
            "current_api_version": CURRENT_API_VERSION,
            "resource": settings.SEVERINO_API_RESOURCE,
            "actor": request.principal.actor,
            "granted": sorted(granted(request.token_claims)),
            "links": links,
        }
    )


@_endpoint(("GET",))
def capabilities(request, version: int):
    """Every capability HQ has, flagged by whether this token may run it.

    The whole registry is returned, not just the permitted slice: a client
    being told a capability exists but is not granted is the message that gets
    someone to fix a scope, where an empty list looks like a broken server.
    """

    described = describe_capabilities()
    held = granted(request.token_claims)
    return _ok(
        {
            "schema_version": described["schema_version"],
            "capabilities": [
                {
                    **spec,
                    "permitted": set(spec["required_capabilities"]) <= held,
                    "idempotency_key_required": (
                        version >= 2 and spec["effect"] != "read"
                    ),
                    "request_schema": _request_schema(spec),
                }
                for spec in described["capabilities"]
            ],
        }
    )


@_endpoint(("GET",))
def resources(request, version: int):
    """Every readable resource, including operations this token may use."""

    described = describe_resources()
    held = granted(request.token_claims)
    return _ok(
        {
            "schema_version": described["schema_version"],
            "resources": [
                {
                    **spec,
                    "permitted": set(spec["required_capabilities"]) <= held,
                }
                for spec in described["resources"]
            ],
        }
    )


def _resource_failure(exc: Exception) -> HttpResponse:
    if isinstance(exc, UnknownResource):
        return _fail(str(exc), code="unknown_resource", status=404)
    if isinstance(exc, ResourceNotFound):
        return _fail(str(exc), code="not_found", status=404)
    if isinstance(exc, UnsupportedResourceOperation):
        return _fail(str(exc), code="unsupported_operation", status=405)
    if isinstance(exc, InvalidResourceInput):
        return _fail(
            str(exc), code="invalid_input", status=400, details=exc.errors
        )
    if isinstance(exc, AuthorizationError):
        return _fail(exc.reason, code=exc.code, status=403)
    raise exc


@_endpoint(("GET",))
def resource_list(request, version: int, name: str):
    """List one resource through its declared, schema-validated query."""

    filters: dict[str, str] = {}
    for key, values in request.GET.lists():
        if len(values) != 1:
            return _fail(
                f"Query field {key!r} may appear only once.",
                code="invalid_input",
                status=400,
            )
        filters[key] = values[0]
    try:
        return _ok(
            list_application_resource(
                name, filters, principal=request.principal, strict=False
            )
        )
    except (
        AuthorizationError,
        InvalidResourceInput,
        UnknownResource,
        UnsupportedResourceOperation,
    ) as exc:
        return _resource_failure(exc)


@_endpoint(("GET",))
def resource_detail(request, version: int, name: str, identifier: str):
    """Get one resource record through its declared identifier contract."""

    try:
        return _ok(
            get_application_resource(
                name, identifier, principal=request.principal, strict=False
            )
        )
    except (
        AuthorizationError,
        InvalidResourceInput,
        ResourceNotFound,
        UnknownResource,
        UnsupportedResourceOperation,
    ) as exc:
        return _resource_failure(exc)


class EnvelopeError(Exception):
    """A request body that never reaches a capability, and why."""

    def __init__(self, message: str, *, code: str = "invalid_input", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Envelope:
    """The three things a capability call arrives wrapped in."""

    command: dict[str, Any]
    target: str | int | None
    expected_updated_at: str | None


@dataclass(frozen=True)
class Dialect:
    """How one API version reads a request body.

    There is one axis here, not seven. v2 checks the envelope -- media type,
    JSON strictness, unknown fields, field types, a retry key on every write --
    and v1 checks none of it. Stating that once, as data, is the difference
    between adding v3 and auditing a function for every place a version number
    was compared.

    v1 is frozen rather than improved: a Shortcut written against it six months
    ago still works, which is the whole reason the version sits in the path.
    """

    strict: bool


DIALECTS = {1: Dialect(strict=False), 2: Dialect(strict=True)}

ENVELOPE_FIELDS = frozenset({"payload", "target", "expected_updated_at"})


def _body_json(request, dialect: Dialect) -> dict[str, Any]:
    """The request body as a JSON object, or an EnvelopeError saying why not."""

    if dialect.strict and request.content_type != "application/json":
        raise EnvelopeError(
            "Content-Type must be application/json.",
            code="unsupported_media_type",
            status=415,
        )
    try:
        body = request.body
    except RequestDataTooBig:
        raise EnvelopeError(
            "Request body exceeds this deployment's safety limit.",
            code="request_too_large",
            status=413,
        ) from None
    if not body:
        return {}
    try:
        if dialect.strict:
            payload = json.loads(
                body,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_json_object,
            )
        else:
            payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise EnvelopeError(
            "Request body is not valid JSON.", code="invalid_json"
        ) from None
    if not isinstance(payload, dict):
        raise EnvelopeError(
            "Request body must be a JSON object.", code="invalid_json"
        )
    return payload


def _parse_envelope(request, dialect: Dialect) -> tuple[dict[str, Any], Envelope]:
    """Validate the wrapper so the capability only ever sees a real command.

    Returns the raw body alongside the parsed envelope because the idempotency
    fingerprint is taken over what was actually sent, not over what survived
    interpretation -- two different bodies that normalise to the same command
    are still two different requests.
    """

    payload = _body_json(request, dialect)

    if dialect.strict:
        unknown = payload.keys() - ENVELOPE_FIELDS
        if unknown:
            raise EnvelopeError(
                f"Unknown request fields: {', '.join(sorted(unknown))}."
            )

    command = payload.get("payload", {}) if dialect.strict else payload.get("payload") or {}
    if not isinstance(command, dict):
        raise EnvelopeError("payload must be a JSON object.")

    target = payload.get("target")
    expected_updated_at = payload.get("expected_updated_at")
    if dialect.strict:
        # bool before (str, int): in Python a bool *is* an int, and a target of
        # ``true`` is a client bug worth naming rather than a record id of 1.
        if target is not None and (
            isinstance(target, bool) or not isinstance(target, (str, int))
        ):
            raise EnvelopeError("target must be a string or integer.")
        if expected_updated_at is not None and not isinstance(expected_updated_at, str):
            raise EnvelopeError("expected_updated_at must be a string.")

    return payload, Envelope(command, target, expected_updated_at)


@_endpoint(("POST",))
def execute(request, name: str, version: int):
    """Run one HQ capability, replaying machine writes by idempotency key."""

    dialect = DIALECTS.get(version, DIALECTS[CURRENT_API_VERSION])
    try:
        payload, envelope = _parse_envelope(request, dialect)
    except EnvelopeError as exc:
        return _fail(exc.message, code=exc.code, status=exc.status)

    def run() -> tuple[dict[str, Any], int]:
        result = execute_capability(
            name,
            envelope.command,
            principal=request.principal,
            target=envelope.target,
            expected_updated_at=envelope.expected_updated_at,
        )
        if result.get("ok", False):
            return (
                {
                    "ok": True,
                    "data": {key: value for key, value in result.items() if key != "ok"},
                },
                200,
            )
        error = result.get("error", {})
        code = error.get("code", "operation_failed")
        detail = {
            "code": code,
            "message": error.get(
                "message", "The capability could not be executed."
            ),
        }
        if error.get("details") is not None:
            detail["details"] = error["details"]
        return {"ok": False, "error": detail}, CAPABILITY_STATUS.get(code, 400)

    spec = capability_registry().get(name)
    if spec is None or spec.effect == "read":
        response_payload, status = run()
        return _json(response_payload, status=status)

    # Reject authority before reserving a retry key. A denied request has not
    # begun an operation and must not poison that key if the client's grant is
    # corrected later.
    try:
        authorize_capability(spec, request.principal)
    except AuthorizationError as exc:
        return _fail(exc.reason, code=exc.code, status=403)

    key = request.headers.get("Idempotency-Key", "")
    if dialect.strict and not key:
        return _fail(
            "Idempotency-Key is required for capabilities that change state.",
            code="idempotency_key_required",
            status=400,
        )
    if not key:
        response_payload, status = run()
        return _json(response_payload, status=status)

    try:
        key = validate_key(key)
        response_payload, status, replayed = execute_once(
            actor=request.principal.actor,
            key=key,
            request_sha256=request_fingerprint(name, payload, api_version=version),
            operation=run,
        )
    except InvalidIdempotencyKey as exc:
        return _fail(exc.reason, code="invalid_idempotency_key", status=400)
    except IdempotencyConflict as exc:
        return _fail(exc.reason, code="idempotency_conflict", status=409)

    response = _json(response_payload, status=status)
    if replayed:
        response["Idempotency-Replayed"] = "true"
    return response
