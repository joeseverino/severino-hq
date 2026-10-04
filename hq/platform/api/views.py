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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Concatenate, Literal, NotRequired, TypedDict, cast

from pydantic import ConfigDict, with_config

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from hq.platform.application.agent_access import agents_paused
from hq.platform.application.agent_registry import observe
from hq.platform.application.denials import record_denial
from hq.platform.core.network import client_ip
from hq.platform.application.capabilities import (
    CapabilityDescription,
    authorize_capability,
    capability_registry,
    describe_capabilities,
    execute_capability,
)
from hq.platform.application.connections import describe_connections, list_connections
from hq.platform.application.findings import findings as application_findings
from hq.platform.application.topology import topology as application_topology
from hq.platform.application.security import AuthorizationError, Capability, Principal, web_principal
from hq.platform.application.resources import (
    InvalidResourceInput,
    ResourceDescription,
    ResourceNotFound,
    UnknownResource,
    UnsupportedResourceOperation,
    describe_resources,
    get_resource as get_application_resource,
    list_resource as list_application_resource,
)

from hq.platform.application.idempotency import (
    IdempotencyConflict,
    InvalidIdempotencyKey,
    execute_once,
    request_fingerprint,
    validate_key,
)
from .security import TokenError, api_principal, granted, is_configured, verify

CURRENT_API_VERSION = 2

REALM = 'Bearer realm="Severino HQ"'

class APIRequest(HttpRequest):
    """A request `_endpoint` has authenticated: who is calling, and with what."""

    principal: Principal
    token_claims: dict[str, Any]


View = Callable[Concatenate[HttpRequest, ...], HttpResponse]
APIView = Callable[Concatenate[APIRequest, ...], HttpResponse]


# What these views answer, typed once: the OpenAPI document (hq_api/openapi.py)
# derives its schemas from these, and its contract tests hold real responses
# to them.
class ErrorDetail(TypedDict):
    code: str
    message: str
    details: NotRequired[Any]


class Failure(TypedDict):
    ok: Literal[False]
    error: ErrorDetail


class RootData(TypedDict):
    service: str
    api_version: int
    resource: str
    actor: str
    granted: list[str]
    links: dict[str, str]


class CapabilityEntry(CapabilityDescription):
    permitted: bool
    idempotency_key_required: bool
    request_schema: dict[str, Any]


class CapabilityCatalog(TypedDict):
    schema_version: int
    capabilities: list[CapabilityEntry]


class ResourceEntry(ResourceDescription):
    permitted: bool


class ResourceCatalog(TypedDict):
    schema_version: int
    resources: list[ResourceEntry]


class ConnectionCatalog(TypedDict):
    schema_version: int
    connections: list[dict[str, Any]]
    groups: list[dict[str, Any]]


@with_config(ConfigDict(extra="allow"))
class ResourceCollection(TypedDict):
    """Every list guarantees items/count and may include projection metadata."""

    items: list[dict[str, Any]]
    count: int


# An application projection with its own schema_version; HQ types it there.
Projection = dict[str, Any]


# HQ answers a failed capability with its own error code. Mapping the ones that
# mean something other than "you sent nonsense" keeps a client on HTTP status
# alone for control flow, which is all a Shortcut can branch on comfortably.
CAPABILITY_STATUS = {
    "unknown_capability": 404,
    "forbidden": 403,
    "operation_failed": 409,
    "idempotency_conflict": 409,
    # Nothing is wrong with the request. This client is holding more decisions
    # than a person has answered, so the honest status is the one that says
    # "later", not the one that says "malformed".
    "too_many_pending_approvals": 429,
}


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("Invalid JSON constant")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
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


def _permission_catalog(
    specs: list[dict[str, Any]], held: set[str] | frozenset[str]
) -> list[dict[str, Any]]:
    """Annotate static registry entries without duplicating adapter policy."""

    return [
        {
            **spec,
            "permitted": set(spec["required_capabilities"]) <= held,
        }
        for spec in specs
    ]


def _fail(message: str, *, code: str, status: int, details: Any = None) -> HttpResponse:
    error: ErrorDetail = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    failure: Failure = {"ok": False, "error": error}
    response = _json(dict(failure), status=status)
    if status == 401:
        # A native client cannot use an HTML login page. Saying *how* to
        # authenticate is the difference between a retryable failure and a
        # Shortcut that silently shows a wall of markup.
        response["WWW-Authenticate"] = REALM
    return response


def _principal(request: HttpRequest) -> tuple[Principal, dict[str, Any]]:
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise TokenError("An access token is required.")
    claims = verify(value.strip())
    return api_principal(claims), claims


def _operator(request: HttpRequest) -> Principal | None:
    """The signed-in operator, for a request with a session and no token."""

    if "Authorization" in request.headers:
        return None
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return web_principal(user)


def _request_schema(spec: Mapping[str, Any]) -> dict[str, Any]:
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
    required: list[str] = []
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
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
    if definitions := spec["input_schema"].get("$defs"):
        schema["$defs"] = definitions
    return schema


# Every endpoint can answer these before its view runs: an unverified token,
# a refused grant or paused agents, a wrong method, an unconfigured surface.
ENDPOINT_ERRORS = (401, 403, 405, 503)


def _endpoint(
    methods: tuple[str, ...],
    *,
    data: Any,
    errors: tuple[int, ...] = (),
    operator_session: bool = False,
) -> Callable[[APIView], View]:
    """Authenticate, then put every failure in the same envelope.

    CSRF-exempt by construction rather than by concession: these views read the
    Authorization header and never the session cookie, so a browser cannot make
    an authenticated request to them at all. ``operator_session`` is the one
    exception, for GET views only: a signed-in operator's session, holding
    READ, is accepted as well, and works while the token surface is
    unconfigured because it does not depend on it.

    ``data`` is the type a success carries under ``data`` (None: the body is
    not enveloped) and ``errors`` the statuses the view adds to
    ENDPOINT_ERRORS. Both are read by hq_api/openapi.py.
    """

    if operator_session and set(methods) - {"GET", "HEAD"}:
        raise ValueError("A session is accepted only on a safe method.")

    def decorate(view: APIView) -> View:
        @csrf_exempt
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            operator = _operator(request) if operator_session else None
            if operator is None and not is_configured():
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
            if operator is not None:
                try:
                    operator.require(Capability.READ)
                except AuthorizationError as exc:
                    return _fail(exc.reason, code=exc.code, status=403)
                authenticated = cast(APIRequest, request)
                authenticated.principal = operator
                authenticated.token_claims = {}
                return view(authenticated, *args, **kwargs)
            try:
                principal, claims = _principal(request)
            except (TokenError, AuthorizationError) as exc:
                # No identity was established, so these are counted per source.
                record_denial(
                    interface="api",
                    reason=exc.code,
                    source=client_ip(request),
                    authenticated=False,
                )
                status = 401 if isinstance(exc, TokenError) else 403
                return _fail(exc.reason, code=exc.code, status=status)
            # Before the brake, so a paused agent is still registered.
            observe(principal)
            # The same brake /mcp/ applies, and for the same callers: every
            # token here is a Pocket ID client, and the agents hold exactly
            # these tokens.
            # After authentication, so only a valid caller learns it; fails
            # closed, because agents_paused() does.
            if agents_paused():
                record_denial(
                    interface="api",
                    reason="agents_paused",
                    actor=principal.actor,
                    source=client_ip(request),
                )
                return _fail(
                    "Agents are paused by the operator.",
                    code="agents_paused",
                    status=403,
                )
            # The one place these are set; every endpoint reads them.
            authenticated = cast(APIRequest, request)
            authenticated.principal = principal
            authenticated.token_claims = claims
            return view(authenticated, *args, **kwargs)

        wrapper.__name__ = view.__name__
        wrapper.__doc__ = view.__doc__
        # A fact a test can read, rather than one a reviewer has to notice.
        # `/api/` is exempt from the login *redirect*, so a view added here
        # without this decorator is not merely unprotected by convention: it
        # is served to anyone who asks. `core.tests.test_security` walks these routes
        # and fails if one lacks the mark.
        setattr(wrapper, "__hq_authenticated__", True)
        setattr(wrapper, "__hq_methods__", methods)
        setattr(wrapper, "__hq_data__", data)
        setattr(wrapper, "__hq_errors__", tuple(sorted({*ENDPOINT_ERRORS, *errors})))
        setattr(wrapper, "__hq_operator_session__", operator_session)
        return wrapper

    return decorate


@_endpoint(("GET",), data=RootData)
def root(request: APIRequest) -> HttpResponse:
    """What this is, and what the presented credential may actually do."""

    links = {
        name: reverse(f"hq_api:{name}")
        for name in ("capabilities", "resources", "connections", "topology", "findings", "openapi")
    }
    data: RootData = {
        "service": "severino-hq",
        "api_version": CURRENT_API_VERSION,
        "resource": settings.SEVERINO_API_RESOURCE,
        "actor": request.principal.actor,
        "granted": sorted(granted(request.token_claims)),
        "links": links,
    }
    return _ok(data)


@_endpoint(("GET",), data=None, operator_session=True)
def openapi(request: APIRequest) -> HttpResponse:
    """This API as an OpenAPI 3.2 document, derived from its routes and registries.

    Never anonymous: it names every capability, grant and resource kind, which
    /capabilities/ already keeps from anonymous callers. The signed-in operator
    reads it too (the reference page at /api/docs/), including where no API
    resource is configured: it describes the code, not a credential.
    """

    from .openapi import document

    return _json(document())


@_endpoint(("GET",), data=CapabilityCatalog)
def capabilities(request: APIRequest) -> HttpResponse:
    """Every capability HQ has, flagged by whether this token may run it.

    The whole registry is returned, not just the permitted slice: a client
    being told a capability exists but is not granted is the message that gets
    someone to fix a scope, where an empty list looks like a broken server.
    """

    described = describe_capabilities()
    held = granted(request.token_claims)
    specs: list[CapabilityDescription] = described["capabilities"]
    catalog: CapabilityCatalog = {
        "schema_version": described["schema_version"],
        "capabilities": [
            {
                **spec,
                "permitted": set(spec["required_capabilities"]) <= held,
                "idempotency_key_required": spec["effect"] != "read",
                "request_schema": _request_schema(spec),
            }
            for spec in specs
        ],
    }
    return _ok(catalog)


@_endpoint(("GET",), data=ResourceCatalog)
def resources(request: APIRequest) -> HttpResponse:
    """Every readable resource, including operations this token may use."""

    described = describe_resources()
    held = granted(request.token_claims)
    specs: list[ResourceDescription] = described["resources"]
    catalog: ResourceCatalog = {
        "schema_version": described["schema_version"],
        "resources": [
            {**spec, "permitted": set(spec["required_capabilities"]) <= held}
            for spec in specs
        ],
    }
    return _ok(catalog)


def _projection(
    serve: Callable[..., Any], query_fields: tuple[str, ...], name: str, doc: str
) -> View:
    """One principal-scoped projection, served with declared narrowing inputs.

    `topology` and `findings` are the same adapter: authorize, read one query
    parameters, hand them to the application layer, and turn a refusal into a
    403. An adapter that adds no behaviour of its own is not copied once per
    read model.

    An unrecognized filter value is the application's business, not the
    transport's: both projections answer it by returning everything and saying
    which filter they applied, so a client can tell "matched nothing" from
    "never applied".
    """

    def view(request: APIRequest) -> HttpResponse:
        try:
            return _ok(
                serve(
                    principal=request.principal,
                    **{
                        field: request.GET.get(field, "").strip()
                        for field in query_fields
                    },
                )
            )
        except AuthorizationError as exc:
            return _fail(exc.reason, code=exc.code, status=403)

    # Named explicitly: `_endpoint` copies these onto its wrapper, and the
    # route-walking security test reads them.
    view.__name__ = name
    view.__doc__ = doc
    served = _endpoint(("GET",), data=Projection)(view)
    # The narrowing inputs, for the OpenAPI document's query parameters.
    setattr(served, "__hq_query_fields__", query_fields)
    return served


topology = _projection(
    application_topology,
    ("lens", "focus", "direction", "depth"),
    "topology",
    """The live permitted infrastructure graph and its canonical actions.

    `?lens=` narrows to a standing question. `?focus=`, `direction`, and
    bounded `depth` trace a dependency neighborhood inside that projection.
    """,
)

findings = _projection(
    application_findings,
    ("rule",),
    "findings",
    """What HQ currently claims is wrong, with the evidence and a remedy.

    Derived from the same projection as the topology and narrowed the same way,
    so a finding can never name something the token could not already read. A
    remedy is a reference to an existing capability, never a new route.
    """,
)


@_endpoint(("GET",), data=ConnectionCatalog)
def connections(request: APIRequest) -> HttpResponse:
    """Connection contracts plus the safe state this token may inspect."""

    described = describe_connections()
    held = granted(request.token_claims)
    state = list_connections(principal=request.principal)
    catalog: ConnectionCatalog = {
        "schema_version": described["schema_version"],
        "connections": _permission_catalog(described["connections"], held),
        "groups": state["groups"],
    }
    return _ok(catalog)


def _resource_failure(exc: Exception) -> HttpResponse:
    # Every branch answers with the exception's curated ``reason``, never
    # ``str(exc)``: the message a client reads is written by HQ, not relayed.
    if isinstance(exc, UnknownResource):
        return _fail(exc.reason, code="unknown_resource", status=404)
    if isinstance(exc, ResourceNotFound):
        return _fail(exc.reason, code="not_found", status=404)
    if isinstance(exc, UnsupportedResourceOperation):
        return _fail(exc.reason, code="unsupported_operation", status=405)
    if isinstance(exc, InvalidResourceInput):
        # ``details`` stays the pydantic error list: a documented part of the
        # invalid_input contract, describing the request the client sent.
        return _fail(
            exc.reason, code="invalid_input", status=400, details=exc.errors
        )
    if isinstance(exc, AuthorizationError):
        return _fail(exc.reason, code=exc.code, status=403)
    raise exc


# What _resource_failure answers with.
RESOURCE_ERRORS = (400, 404, 405)


@_endpoint(("GET",), data=ResourceCollection, errors=RESOURCE_ERRORS)
def resource_list(request: APIRequest, name: str) -> HttpResponse:
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


@_endpoint(("GET",), data=dict[str, Any], errors=RESOURCE_ERRORS)
def resource_detail(request: APIRequest, name: str, identifier: str) -> HttpResponse:
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

    def __init__(
        self, message: str, *, code: str = "invalid_input", status: int = 400
    ) -> None:
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


ENVELOPE_FIELDS = frozenset({"payload", "target", "expected_updated_at"})


def _body_json(request: HttpRequest) -> dict[str, Any]:
    """The request body as a JSON object, or an EnvelopeError saying why not."""

    if request.content_type != "application/json":
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
        payload = json.loads(
            body,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_json_object,
        )
    except (ValueError, UnicodeDecodeError):
        raise EnvelopeError(
            "Request body is not valid JSON.", code="invalid_json"
        ) from None
    if not isinstance(payload, dict):
        raise EnvelopeError(
            "Request body must be a JSON object.", code="invalid_json"
        )
    return payload


def _parse_envelope(request: HttpRequest) -> tuple[dict[str, Any], Envelope]:
    """Validate the wrapper so the capability only ever sees a real command.

    Returns the raw body alongside the parsed envelope because the idempotency
    fingerprint is taken over what was actually sent, not over what survived
    interpretation: two different bodies that normalise to the same command
    are still two different requests.
    """

    payload = _body_json(request)

    unknown = payload.keys() - ENVELOPE_FIELDS
    if unknown:
        raise EnvelopeError(f"Unknown request fields: {', '.join(sorted(unknown))}.")

    command = payload.get("payload", {})
    if not isinstance(command, dict):
        raise EnvelopeError("payload must be a JSON object.")

    target = payload.get("target")
    expected_updated_at = payload.get("expected_updated_at")
    # bool before (str, int): in Python a bool *is* an int, and a target of
    # ``true`` is a client bug worth naming rather than a record id of 1.
    if target is not None and (isinstance(target, bool) or not isinstance(target, (str, int))):
        raise EnvelopeError("target must be a string or integer.")
    if expected_updated_at is not None and not isinstance(expected_updated_at, str):
        raise EnvelopeError("expected_updated_at must be a string.")

    return payload, Envelope(command, target, expected_updated_at)


# The envelope's own refusals (malformed, too large, wrong media type, a missing
# or reused retry key) and every status a capability's error code maps to.
EXECUTE_ERRORS = (400, 409, 413, 415, *CAPABILITY_STATUS.values())


@_endpoint(("POST",), data=dict[str, Any], errors=EXECUTE_ERRORS)
def execute(request: APIRequest, name: str) -> HttpResponse:
    """Run one HQ capability, replaying machine writes by idempotency key."""

    try:
        payload, envelope = _parse_envelope(request)
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
    if not key:
        return _fail(
            "Idempotency-Key is required for capabilities that change state.",
            code="idempotency_key_required",
            status=400,
        )

    try:
        key = validate_key(key)
        response_payload, status, replayed = execute_once(
            actor=request.principal.actor,
            key=key,
            request_sha256=request_fingerprint(name, payload, api_version=CURRENT_API_VERSION),
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
