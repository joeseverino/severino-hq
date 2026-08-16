"""Version 1 of the machine-client transport.

This adapter adds no capability, no domain model, and no business rule. Every
command it runs is already in HQ's registry, which is what keeps the web UI,
the CLI, MCP and a Shortcut from drifting into four behaviours. A phone cannot
develop its own idea of what a workout is.

The version is in the path, not a header: a Shortcut on a phone you have not
opened in six months is a normal state, and a URL that quietly changed meaning
is the failure this prevents.
"""

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from application.capabilities import describe_capabilities, execute_capability
from application.security import AuthorizationError

from .security import TokenError, api_principal, granted, is_configured, verify

API_VERSION = 1

REALM = 'Bearer realm="Severino HQ"'

# HQ answers a failed capability with its own error code. Mapping the ones that
# mean something other than "you sent nonsense" keeps a client on HTTP status
# alone for control flow, which is all a Shortcut can branch on comfortably.
CAPABILITY_STATUS = {
    "unknown_capability": 404,
    "forbidden": 403,
    "operation_failed": 409,
}


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
                return _fail(
                    f"{request.method} is not allowed here.",
                    code="method_not_allowed",
                    status=405,
                )
            try:
                principal, claims = _principal(request)
            except TokenError as exc:
                return _fail(str(exc), code=exc.code, status=401)
            except AuthorizationError as exc:
                return _fail(str(exc), code=exc.code, status=403)
            request.principal = principal
            request.token_claims = claims
            return view(request, *args, **kwargs)

        wrapper.__name__ = view.__name__
        wrapper.__doc__ = view.__doc__
        return wrapper

    return decorate


@_endpoint(("GET",))
def root(request):
    """What this is, and what the presented credential may actually do."""

    return _ok(
        {
            "service": "severino-hq",
            "api_version": API_VERSION,
            "resource": settings.SEVERINO_API_RESOURCE,
            "actor": request.principal.actor,
            "granted": sorted(granted(request.token_claims)),
        }
    )


@_endpoint(("GET",))
def capabilities(request):
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
                {**spec, "permitted": set(spec["required_capabilities"]) <= held}
                for spec in described["capabilities"]
            ],
        }
    )


@_endpoint(("POST",))
def execute(request, name: str):
    """Run one HQ capability. This is what a Shortcut actually calls.

    No idempotency key. The one write this exists for -- an import -- is
    already idempotent by content hash in the domain, so a Shortcut retried on
    a dropped connection reports a duplicate rather than creating one. A key
    here would be a second mechanism guarding something already guarded.
    """

    if not request.body:
        payload: dict[str, Any] = {}
    else:
        try:
            payload = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            return _fail(
                "Request body is not valid JSON.", code="invalid_json", status=400
            )
    if not isinstance(payload, dict):
        return _fail(
            "Request body must be a JSON object.", code="invalid_json", status=400
        )

    command = payload.get("payload") or {}
    if not isinstance(command, dict):
        return _fail(
            "payload must be a JSON object.", code="invalid_input", status=400
        )

    result = execute_capability(
        name,
        command,
        principal=request.principal,
        target=payload.get("target"),
        expected_updated_at=payload.get("expected_updated_at"),
    )
    if not result.get("ok", False):
        error = result.get("error", {})
        code = error.get("code", "operation_failed")
        return _fail(
            error.get("message", "The capability could not be executed."),
            code=code,
            status=CAPABILITY_STATUS.get(code, 400),
            details=error.get("details"),
        )
    return _ok({key: value for key, value in result.items() if key != "ok"})
