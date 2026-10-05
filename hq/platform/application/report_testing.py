"""Controller reports for tests, complete as the bridge contract states them.

A test names the members it is about; the rest take the values a controller
sends when it has nothing to say. Each report is held to the contract's schema
before it is recorded, so a test cannot exercise a shape the bridge refuses.
"""

from __future__ import annotations

from typing import Any

from hq.domains.control_plane.bridge_contract import departs

from .inventory import record_connections
from .security import cli_principal

_QUIET_CONNECTION = {
    "provider": "",
    "endpoint": "",
    "manages": False,
    "probed": True,
    "ok": True,
    "detail": "",
    "reaches": [],
}


def _held(schema: str, value: dict[str, Any]) -> dict[str, Any]:
    problem = departs(schema, value)
    if problem is not None:
        raise AssertionError(problem)
    return value


def connection_record(**members: Any) -> dict[str, Any]:
    """One ``ConnectionRecord`` with ``members`` set."""

    return _held("ConnectionRecord", {**_QUIET_CONNECTION, **members})


def report_connections(
    connections: list[dict[str, Any]], *, principal: Any = None, controller_id: str = ""
) -> dict[str, Any]:
    """Record ``connections`` as one controller's report."""

    return record_connections(
        [connection_record(**connection) for connection in connections],
        principal=principal or cli_principal(),
        controller_id=controller_id,
    )


def refused_part(part: str, **members: Any) -> dict[str, Any]:
    """One ``RefusedPart`` with ``members`` set."""

    quiet = {"refusal": "", "reason": "", "scope": "", "connection_ref": ""}
    return _held("RefusedPart", {"part": part, **quiet, **members})
