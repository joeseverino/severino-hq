"""The controller bridge as an ASGI application: the contract's actions over HTTP.

Built from the contract (``bridge_contract.operations``: paths, parameters,
bodies) and ``ACTIONS`` (what each does); import fails if they name different
actions. It has no login, session or CSRF check, because reaching its private
Unix socket is the authorization: docs/Architecture.md#the-controller.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from asgiref.sync import sync_to_async
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from hq.domains.control_plane.bridge_actions import ACTIONS, CONTROLLER, Action
from hq.domains.control_plane.bridge_contract import (
    Operation,
    Parameter,
    declared_names,
    max_body_bytes,
    operations,
)
from hq.domains.control_plane.secret_shapes import struck
from hq.platform.application import refused_reports
from hq.platform.application.security import CONTROLLER_INTERFACE
from hq.platform.application.ui import ended
from hq.platform.core.attribution import operation_context
from hq.platform.core.outside_requests import thread_of_its_own

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
    # The bridge's one door for secrets: nothing past it holds one. Struck
    # rather than refused, since an outsider writes some of these strings.
    # Held to the contract as struck, so what an action reads conforms.
    payload, strikes = struck(payload, declared_names())
    for pointer, what in strikes:
        logger.warning("bridge.secret_struck action=%s at=%s held=%s", operation.name, pointer, what)
    violation = operation.violation(payload)
    if violation is not None:
        raise Refused(HTTPStatus.UNPROCESSABLE_ENTITY, violation)
    return payload


def _refuse_secrets(operation: Operation, parameters: dict[str, Any]) -> None:
    """A query holding a secret is refused: only the controller writes one,
    and an identifier struck would name something else."""

    strikes = struck(parameters, declared_names())[1]
    if strikes:
        pointer, what = strikes[0]
        raise Refused(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"The {operation.name} query at {pointer} holds {what}, and HQ takes in no secret.",
        )


def _locations(exc: ValidationError) -> str:
    """Where a validation error's input failed and how, without the input."""

    said = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'input'} ({error['type']})"
        for error in exc.errors(include_url=False, include_context=False, include_input=False)[:5]
    )
    return ended(f"The input does not validate at {said}")


def _run(action: Action, parameters: dict[str, Any], payload: Any) -> bytes:
    # The bridge's door: whatever an action writes is the controller's.
    with operation_context(interface=CONTROLLER_INTERFACE, actor=CONTROLLER, operation=f"bridge.{action.name}"):
        answer = json.dumps(action.run(parameters, payload), sort_keys=True).encode()
        refused_reports.accepted(action.name, _controller(parameters))
        return answer


def _controller(parameters: dict[str, Any]) -> str:
    named = parameters.get("controller-id")
    return named if isinstance(named, str) else ""


def _refused(action: Action, controller_id: str, refusal: Refused) -> None:
    # Kept apart from the action, which a refusal runs nothing of, so a
    # controller whose reports stop landing is a finding and not a silence.
    with operation_context(interface=CONTROLLER_INTERFACE, actor=CONTROLLER, operation=f"bridge.{action.name}.refused"):
        refused_reports.note(action.name, controller_id, int(refusal.status), refusal.detail)


def endpoint(action: Action, operation: Operation) -> Callable[[Request], Awaitable[Response]]:
    async def call(request: Request) -> Response:
        # Named only once the query is held to the contract and holds no secret.
        controller_id = ""
        try:
            parameters = parameters_of(operation, request)
            _refuse_secrets(operation, parameters)
            controller_id = _controller(parameters)
            payload = await payload_of(operation, request)
            # Calls in flight together each hold their own connection, and
            # SQLite orders their writes.
            async with thread_of_its_own():
                answer = await sync_to_async(_run)(action, parameters, payload)
        except Refused as refusal:
            async with thread_of_its_own():
                await sync_to_async(_refused)(action, controller_id, refusal)
            return problem(refusal.status, refusal.detail)
        except ClientDisconnect:
            return problem(HTTPStatus.BAD_REQUEST, "The caller went away.")
        except ValidationError as exc:
            # Its text repeats the input it refused.
            return problem(HTTPStatus.BAD_REQUEST, _locations(exc))
        except ValueError as exc:
            # What an action raises for input it will not take, said in its
            # own words. A value it repeats is one the controller sent, struck
            # of every secret at the door above, so the echo holds none.
            return problem(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:  # the log keeps the trace
            # Its text can be anything inside HQ: a query, a path, a value.
            logger.exception("bridge.action_failed action=%s", action.name)
            return problem(HTTPStatus.INTERNAL_SERVER_ERROR, f"HQ failed running {action.name}; its log holds why.")
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


def _routes(actions: tuple[Action, ...]) -> list[Route]:
    declared = operations()
    by_name = {action.name: action for action in actions}
    if len(by_name) != len(actions) or set(by_name) != set(declared):
        raise RuntimeError(
            "Bridge actions and the contract's paths differ: " + ", ".join(sorted(set(by_name) ^ set(declared)))
        )
    return [Route(f"/{name}", endpoint(by_name[name], declared[name]), methods=["POST"]) for name in sorted(declared)]


def bridge(actions: tuple[Action, ...] = ACTIONS) -> UnixSocketOnly:
    """The bridge application serving ``actions``, which name exactly the contract's paths.

    Served with ``ACTIONS``; a test hands actions that record they ran.
    """

    return UnixSocketOnly(Starlette(routes=_routes(actions), exception_handlers={HTTPException: _http_error}))


application = bridge()
