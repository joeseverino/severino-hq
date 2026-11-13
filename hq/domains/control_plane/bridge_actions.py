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
from hq.platform.application.certificates import material_for
from hq.platform.application.controller import (
    ControllerReport,
    claim_next_operation,
    controller_registry,
    peek_next_operation,
    renew_operation,
    report_operation,
    schedule_automatic_operations,
)
from hq.platform.application.glance import dashboard_refresh_plan, record_dashboard_observations
from hq.platform.application.infrastructure import controller_contract
from hq.platform.application.inventory import record_connections, record_step_failures
from hq.platform.application.record_errors import CertificateError
from hq.platform.application.refused_reports import READINGS_ACTION
from hq.platform.application.scheduled_work import run as run_scheduled
from hq.platform.application.security import internal_principal
from hq.platform.application.sweep import record_sweep

Parameters = Mapping[str, Any]
Run = Callable[[Parameters, Any], Any]

# Who a controller's report is recorded under, in the audit trail.
CONTROLLER = "controller"


def _capabilities(parameters: Parameters) -> tuple[tuple[str, str], ...]:
    parsed = tuple(tuple(value.split(":", 1)) for value in parameters["capability"])
    if any(len(item) != 2 or not all(item) for item in parsed):
        raise ValueError("Capabilities must use kind:action.")
    return tuple((kind, action) for kind, action in parsed)


def _claim(parameters: Parameters, payload: Any) -> Any:
    # Every applying pass claims, an idle one included, before any work that
    # could end it early, and neither a preflight nor a glance pass does: so
    # this is the controller arriving. A glance pass runs when a person asks,
    # and counted, it would hide a controller that had stopped.
    del payload
    note_controller()
    return claim_next_operation(
        parameters["controller-id"],
        lease_seconds=parameters["lease-seconds"],
        capabilities=_capabilities(parameters),
        attempt=parameters["attempt"] or "",
    )


def _renew(parameters: Parameters, payload: Any) -> Any:
    del payload
    return renew_operation(
        parameters["operation"],
        controller_id=parameters["controller-id"],
        lease_seconds=parameters["lease-seconds"],
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
    # with a contract, and a stored private key, sealed to the controller, is
    # read only by the call that asks for it by name.
    del payload
    try:
        return material_for(parameters["resource"])
    except CertificateError as exc:
        raise ValueError(str(exc)) from exc


def _recorded(record: Callable[..., Any]) -> Run:
    """A controller's report, recorded by HQ itself: the bridge has already
    admitted the controller, and nobody is acting on its behalf."""

    def run(parameters: Parameters, payload: Any) -> Any:
        return record(payload, principal=internal_principal(CONTROLLER), controller_id=parameters["controller-id"])

    return run


def _analytics_plan(parameters: Parameters, payload: Any) -> Any:
    del parameters
    return analytics_plan(payload)


def _sweep_due(parameters: Parameters, payload: Any) -> Any:
    del payload
    return sweep_due(parameters["controller-id"])


def _glance_plan(parameters: Parameters, payload: Any) -> Any:
    del payload
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
    """One thing the controller may ask, and what a person calls its asking:
    the title of every audit row written while it runs (``audit_words.title_of``)."""

    name: str
    title: str
    run: Run


ACTIONS: tuple[Action, ...] = (
    Action("claim", "The controller took the next queued work", _claim),
    Action("peek", "The controller looked for queued work", _peek),
    Action("export", "The controller read what a record declares", _export),
    Action("schedule", "The controller queued the automatic work", _schedule),
    Action("material", "The controller fetched a certificate's key", _material),
    Action(READINGS_ACTION, "The controller read what exists", _recorded(record_sweep)),
    Action("connections", "The controller said what it can reach", _recorded(record_connections)),
    Action("steps", "The controller said what it could not finish", _recorded(record_step_failures)),
    Action("analytics", "The controller sent site analytics", _recorded(record_analytics)),
    Action("analytics-plan", "The controller asked which analytics to read", _analytics_plan),
    Action("sweep-due", "The controller asked which readings are due", _sweep_due),
    Action("glance-plan", "The controller asked which panels to refresh", _glance_plan),
    Action("glance", "The controller refreshed the dashboard", _recorded(record_dashboard_observations)),
    Action("renew", "A worker kept its lease on queued work", _renew),
    Action("report", "The controller said how queued work ended", _report),
    Action("registry", "The controller read what each provider declares", _registry),
    Action("job", "The controller ran scheduled work", _job),
)
