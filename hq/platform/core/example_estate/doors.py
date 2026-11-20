"""The doors the estate's events come through, each bound as its real one binds
it, so the log says how each thing was really done whatever surrounds the
seeding (a test's arrangement, the shell's demo command).

Each binding is one request of its own, under an id of its own, as a browser's
or an agent's call is.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from uuid import uuid7

from django.urls import reverse

from hq.domains.control_plane.bridge_actions import CONTROLLER
from hq.platform.application.security import CONTROLLER_INTERFACE, MCP_INTERFACE, MCP_OPERATION
from hq.platform.core import bindings
from hq.platform.core.attribution import bound
from hq.platform.core.middleware import request_operation

# Where a person's sign-in lands: the identity provider's callback, which
# signs them in and records the accounts it names.
SIGN_IN = "oidc_authentication_callback"


def request(path: str) -> AbstractContextManager[None]:
    """A request for ``path``, through the web or the machine API, named by its route."""

    return request_operation(path, operation_id=uuid7().hex)


def page(route: str, /, **kwargs: Any) -> AbstractContextManager[None]:
    """A request for ``route``, with the arguments its address takes."""

    return request(reverse(route, kwargs=kwargs or None))


def mcp() -> AbstractContextManager[None]:
    """A request to the MCP server, as its mount binds one."""

    return bindings.operation_context(
        interface=MCP_INTERFACE, actor="", operation=MCP_OPERATION, operation_id=uuid7().hex
    )


@contextmanager
def controller(action: str) -> Iterator[None]:
    """The controller's report of ``action``: a request of its own, which the
    bridge binds as the controller's."""

    operation = f"bridge.{action}"
    with (
        bindings.operation_context(
            interface=CONTROLLER_INTERFACE, actor="", operation=operation, operation_id=uuid7().hex
        ),
        bindings.operation_context(interface=CONTROLLER_INTERFACE, actor=CONTROLLER, operation=operation),
    ):
        yield


def stamp(door: AbstractContextManager[None]) -> dict[str, Any]:
    """What ``door`` binds, as an ``audit.stamped`` answer: for a row the
    estate dates after the fact, written outside the door it names."""

    with door:
        context = bound()
    if context is None:
        raise RuntimeError(f"{door!r} bound no operation")
    return {
        "metadata": {"interface": context.interface, "operation": context.operation},
        "operation_id": context.operation_id,
    }
