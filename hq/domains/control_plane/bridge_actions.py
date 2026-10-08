"""What the controller may ask of HQ, one function per bridge action.

Every action is declared once, in ``ACTIONS``: its name and the function that
runs it. The contract (``bridge_contract``) states each action's parameters
and whether it carries a payload; the bridge application parses a request
against that and calls the function with the result, so an action never reads
a request itself.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.analytics import analytics_plan, record_analytics
from hq.platform.application.cadence import note_controller, sweep_due
from hq.platform.application.certificates import CertificateError, material_for
from hq.platform.application.controller import (
    ControllerReport,
    claim_next_operation,
    controller_registry,
    peek_next_operation,
    report_operation,
    schedule_automatic_operations,
)
from hq.platform.application.glance import dashboard_refresh_plan, record_dashboard_observations
from hq.platform.application.infrastructure import controller_contract
from hq.platform.application.inventory import record_connections, record_step_failures
from hq.platform.application.scheduled_work import run as run_scheduled
from hq.platform.application.security import cli_principal
from hq.platform.application.sweep import record_sweep

Parameters = Mapping[str, Any]
Run = Callable[[Parameters, Any], Any]


def _capabilities(parameters: Parameters) -> tuple[tuple[str, str], ...]:
    parsed = tuple(tuple(value.split(":", 1)) for value in parameters["capability"])
    if any(len(item) != 2 or not all(item) for item in parsed):
        raise ValueError("Capabilities must use kind:action.")
    return tuple((kind, action) for kind, action in parsed)


def _claim(parameters: Parameters, payload: Any) -> Any:
    del payload
    return claim_next_operation(
        parameters["controller-id"],
        lease_seconds=parameters["lease-seconds"],
        capabilities=_capabilities(parameters),
    )


def _peek(parameters: Parameters, payload: Any) -> Any:
    del payload
    return peek_next_operation(capabilities=_capabilities(parameters))


def _export(parameters: Parameters, payload: Any) -> Any:
    del payload
    try:
        resource = ManagedResource.objects.get(key=parameters["resource"])
    except ManagedResource.DoesNotExist as exc:
        raise ValueError("Managed resource was not found.") from exc
    return controller_contract(resource)


def _material(parameters: Parameters, payload: Any) -> Any:
    # Its own action rather than a field on the contract: `export` answers
    # with a contract, and a stored private key is read only by the call that
    # asks for it by name.
    del payload
    try:
        return material_for(parameters["resource"])
    except CertificateError as exc:
        raise ValueError(str(exc)) from exc


def _recorded(record: Callable[..., Any]) -> Run:
    """A controller's report, recorded against it as the local operator."""

    def run(parameters: Parameters, payload: Any) -> Any:
        return record(payload, principal=cli_principal(), controller_id=parameters["controller-id"])

    return run


def _analytics_plan(parameters: Parameters, payload: Any) -> Any:
    del parameters
    return analytics_plan(payload)


def _sweep_due(parameters: Parameters, payload: Any) -> Any:
    del payload
    return sweep_due(parameters["controller-id"])


def _glance_plan(parameters: Parameters, payload: Any) -> Any:
    # The first call of every applying run, before any work that could end it
    # early, and one a preflight never makes: so this is the controller
    # arriving, noted before anything it does can go wrong.
    del payload
    note_controller()
    return dashboard_refresh_plan(parameters["controller-id"])


def _registry(parameters: Parameters, payload: Any) -> Any:
    del parameters, payload
    return controller_registry()


def _job(parameters: Parameters, payload: Any) -> Any:
    del payload
    return run_scheduled(parameters["name"])


def _schedule(parameters: Parameters, payload: Any) -> Any:
    del payload
    return schedule_automatic_operations(parameters["controller-id"])


def _report(parameters: Parameters, payload: Any) -> Any:
    # Held to the contract's ``ControllerReport`` before it arrives here.
    return report_operation(
        parameters["operation"], ControllerReport(**payload), controller_id=parameters["controller-id"]
    )


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    run: Run


ACTIONS: tuple[Action, ...] = (
    Action("claim", _claim),
    Action("peek", _peek),
    Action("export", _export),
    Action("schedule", _schedule),
    Action("material", _material),
    Action("inventory", _recorded(record_sweep)),
    Action("connections", _recorded(record_connections)),
    Action("steps", _recorded(record_step_failures)),
    Action("analytics", _recorded(record_analytics)),
    Action("analytics-plan", _analytics_plan),
    Action("sweep-due", _sweep_due),
    Action("glance-plan", _glance_plan),
    Action("glance", _recorded(record_dashboard_observations)),
    Action("report", _report),
    Action("registry", _registry),
    Action("job", _job),
)
