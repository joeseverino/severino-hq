"""Controller reports for tests and the example estate, complete as the bridge contract states them.

A test names the members it is about; the rest take the values a controller
sends when it has nothing to say. Each report is held to the contract's schema
before it is recorded, so a test cannot exercise a shape the bridge refuses.

A report reaches its writer the way the bridge sends it (``bridged``): through
the bridge's own action, which hands the writer its ``Admitted``.
"""

from typing import Any

from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.domains.control_plane.bridge_contract import departs

_QUIET_CONNECTION = {
    "provider": "",
    "endpoint": "",
    "manages": False,
    "probed": True,
    "ok": True,
    "detail": "",
    "reaches": [],
}


def bridged(action: str, payload: Any, *, controller_id: str = "") -> Any:
    """``payload`` reported as the controller ``controller_id`` sends the bridge's ``action``."""

    (run,) = (each.run for each in ACTIONS if each.name == action)
    return run({"controller-id": controller_id}, payload)


def _held(schema: str, value: dict[str, Any]) -> dict[str, Any]:
    problem = departs(schema, value)
    if problem is not None:
        raise AssertionError(problem)
    return value


def connection_record(**members: Any) -> dict[str, Any]:
    """One ``ConnectionRecord`` with ``members`` set."""

    return _held("ConnectionRecord", {**_QUIET_CONNECTION, **members})


def report_connections(connections: list[dict[str, Any]], *, controller_id: str = "") -> dict[str, Any]:
    """Record ``connections`` as one controller's report."""

    return bridged(
        "connections", [connection_record(**connection) for connection in connections], controller_id=controller_id
    )


def refused_part(part: str, **members: Any) -> dict[str, Any]:
    """One ``RefusedPart`` with ``members`` set."""

    quiet = {"refusal": "", "reason": "", "scope": "", "connection_ref": ""}
    return _held("RefusedPart", {"part": part, **quiet, **members})
