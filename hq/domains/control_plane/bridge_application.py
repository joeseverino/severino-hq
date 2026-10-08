"""The controller bridge as an ASGI application: the contract's actions over HTTP.

Built from two declarations and nothing else. The contract
(``bridge_contract.operations``) says which paths exist, which query
parameters each takes and whether it carries a JSON body; ``ACTIONS`` says
what each one does. Import fails if the two name different actions.

This application has no login, no session and no CSRF check: reaching it is
the authorization, and the only way to reach it is the private Unix socket it
is served on (``hq.platform.core.unix_server``). It is not part of the web
application's routing, and it refuses a request that arrived on a network
listener, so mounting it there by mistake serves nothing.

A payload is held to the schema the contract declares for its operation
before any action sees it; one that departs from it is refused whole, with the
JSON Pointer of the member that departs. A refusal is an RFC 9457 problem.
Sizes are bounded in both directions by the contract's ``BridgeBody``.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import close_old_connections
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from hq.domains.control_plane.bridge_actions import ACTIONS, Action
from hq.domains.control_plane.bridge_contract import Operation, Parameter, max_body_bytes, operations

logger = logging.getLogger("severino.bridge")

PROBLEM = "application/problem+json"
# What a problem's detail may carry of an error's own text.
DETAIL_LIMIT = 500


class Refused(Exception):
    """A bridge call HQ will not run, with the status that says why."""

    def __init__(self, status: HTTPStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def problem(status: HTTPStatus, detail: str) -> Response:
    body = {"title": status.phrase, "status": int(status), "detail": detail[:DETAIL_LIMIT]}
    return Response(json.dumps(body), status_code=int(status), media_type=PROBLEM)


def _integer(parameter: Parameter, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise Refused(HTTPStatus.BAD_REQUEST, f"{parameter.name} must be an integer.") from exc
    low, high = parameter.schema.get("minimum"), parameter.schema.get("maximum")
    if (low is not None and value < low) or (high is not None and value > high):
        raise Refused(HTTPStatus.BAD_REQUEST, f"{parameter.name} must be between {low} and {high}.")
    return value


def _one(parameter: Parameter, values: list[str]) -> Any:
    if len(values) > 1:
        raise Refused(HTTPStatus.BAD_REQUEST, f"{parameter.name} is given more than once.")
    if not values:
        if parameter.required:
            raise Refused(HTTPStatus.BAD_REQUEST, f"{parameter.name} is required.")
        return parameter.schema.get("default")
    if parameter.schema.get("type") == "integer":
        return _integer(parameter, values[0])
    return values[0]


def parameters_of(operation: Operation, request: Request) -> dict[str, Any]:
    """The request's query as the contract declares it; anything else is refused."""

    declared = {parameter.name for parameter in operation.parameters}
    unknown = sorted(set(request.query_params.keys()) - declared)
    if unknown:
        raise Refused(HTTPStatus.BAD_REQUEST, f"{operation.name} takes no {unknown[0]}.")
    return {
        parameter.name: (
            request.query_params.getlist(parameter.name)
            if parameter.repeated
            else _one(parameter, request.query_params.getlist(parameter.name))
        )
        for parameter in operation.parameters
    }


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise Refused(HTTPStatus.CONTENT_TOO_LARGE, "The payload is larger than the bridge accepts.")
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > limit:
            raise Refused(HTTPStatus.CONTENT_TOO_LARGE, "The payload is larger than the bridge accepts.")
    return bytes(received)


async def payload_of(operation: Operation, request: Request) -> Any:
    raw = await _body(request, max_body_bytes())
    if not operation.takes_body:
        if raw:
            raise Refused(HTTPStatus.BAD_REQUEST, f"{operation.name} takes no payload.")
        return None
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise Refused(HTTPStatus.BAD_REQUEST, "The payload is not JSON.") from exc
    violation = operation.violation(payload)
    if violation is not None:
        raise Refused(HTTPStatus.UNPROCESSABLE_ENTITY, violation)
    return payload


def _run(action: Action, parameters: dict[str, Any], payload: Any) -> bytes:
    """One action, with a database connection that is its thread's own.

    The work is outside Django's request cycle, so the connection is checked
    before it and released after it, as the request signals would.
    """

    close_old_connections()
    try:
        return json.dumps(action.run(parameters, payload), sort_keys=True).encode()
    finally:
        close_old_connections()


def endpoint(action: Action, operation: Operation) -> Callable[[Request], Awaitable[Response]]:
    async def call(request: Request) -> Response:
        try:
            parameters = parameters_of(operation, request)
            payload = await payload_of(operation, request)
            # A thread per call, as Django's own handler runs a view: calls in
            # flight together each hold their own connection, and SQLite
            # orders their writes.
            async with ThreadSensitiveContext():
                answer = await sync_to_async(_run, thread_sensitive=True)(action, parameters, payload)
        except Refused as refusal:
            return problem(refusal.status, refusal.detail)
        except ClientDisconnect:
            return problem(HTTPStatus.BAD_REQUEST, "The caller went away.")
        except ValueError as exc:
            # What an action raises for input it will not take; pydantic's
            # validation error is one.
            return problem(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # the caller is told, the log keeps the trace
            logger.exception("bridge.action_failed action=%s", action.name)
            return problem(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
        if len(answer) > max_body_bytes():
            return problem(HTTPStatus.INTERNAL_SERVER_ERROR, "The answer is larger than the bridge sends.")
        return Response(answer, media_type="application/json")

    return call


class UnixSocketOnly:
    """Refuse a request that did not arrive on a Unix socket.

    ASGI names a Unix listener as ``(path, None)`` and a network listener as
    ``(host, port)``. The bridge has no other authentication, so a request with
    a port, or with no listener named at all, is not served.
    """

    def __init__(self, application: ASGIApp) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        server = scope.get("server")
        if not server or server[1] is not None:
            await problem(HTTPStatus.FORBIDDEN, "The bridge is served on its Unix socket only.")(scope, receive, send)
            return
        await self.application(scope, receive, send)


async def _http_error(request: Request, exc: Exception) -> Response:
    del request
    status = HTTPStatus(exc.status_code) if isinstance(exc, HTTPException) else HTTPStatus.INTERNAL_SERVER_ERROR
    return problem(status, "The bridge has no such action." if status == HTTPStatus.NOT_FOUND else status.phrase)


def _routes() -> list[Route]:
    declared = operations()
    by_name = {action.name: action for action in ACTIONS}
    if len(by_name) != len(ACTIONS) or set(by_name) != set(declared):
        raise RuntimeError(
            "Bridge actions and the contract's paths differ: "
            + ", ".join(sorted(set(by_name) ^ set(declared)))
        )
    return [
        Route(f"/{name}", endpoint(by_name[name], declared[name]), methods=["POST"])
        for name in sorted(declared)
    ]


application = UnixSocketOnly(Starlette(routes=_routes(), exception_handlers={HTTPException: _http_error}))
