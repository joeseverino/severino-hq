"""What the controller may ask of HQ, one function per bridge action.

Every action is declared once, in ``ACTIONS``: its name and the function that
runs it. ``WORKER_ACTIONS`` are the three HQ's playbook worker's own socket
serves, held to its lane as the controller's are to theirs. The contract (``bridge_contract``) states each action's parameters
and whether it carries a payload; the bridge application parses a request
against that and calls the function with the result, so an action never reads
a request itself.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, final

from django.core.exceptions import ValidationError

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.automation import OPERATION_KIND, SOURCE_KIND
from hq.platform.application.analytics import analytics_plan, record_analytics
from hq.platform.application.cadence import note_automation, note_controller, sweep_due
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
from hq.platform.application.glance_refresh import dashboard_refresh_plan, record_dashboard_observations
from hq.platform.application.inbound import apply_events
from hq.platform.application.infrastructure import controller_contract
from hq.platform.application.inventory import record_connections, record_step_failures
from hq.platform.application.outbound_work import run_for_host
from hq.platform.application.record_errors import CertificateError
from hq.platform.application.refused_reports import READINGS_ACTION
from hq.platform.application.sealing import Sealed, sealer
from hq.platform.application.security import Principal, internal_principal
from hq.platform.application.sweep import record_sweep

Parameters = Mapping[str, Any]
Run = Callable[[Parameters, Any], Any]

# Who a controller's report is recorded under, in the audit trail.
CONTROLLER = "controller"
# The records whose work only HQ's playbook worker claims, renews and reports,
# and only on its own socket: a playbook's runs and the source's scans.
AUTOMATION_KINDS = frozenset({OPERATION_KIND, SOURCE_KIND})


def _capabilities(parameters: Parameters) -> tuple[tuple[str, str], ...]:
    parsed = tuple(tuple(value.split(":", 1)) for value in parameters["capability"])
    if any(len(item) != 2 or not all(item) for item in parsed):
        raise ValueError("Capabilities must use kind:action.")
    return tuple((kind, action) for kind, action in parsed)


@dataclass(frozen=True, slots=True)
class Lane:
    """Whose queued work a bridge socket's caller claims, renews and reports:
    the automation kinds' on the worker's socket, every other kind's on the
    controller's. The socket a call arrives on decides it, never the call."""

    automation: bool

    def holds(self, kind: str) -> bool:
        return (kind in AUTOMATION_KINDS) == self.automation

    def operation(self, operation_id: str) -> str:
        """``operation_id``, refused as not found unless its record's kind is this lane's."""

        try:
            kind = OperationRequest.objects.filter(pk=operation_id).values_list("resource__kind", flat=True).first()
        except ValueError, ValidationError:
            kind = None
        if kind is None or not self.holds(kind):
            raise ValueError(f"Operation {operation_id!r} was not found.")
        return operation_id


CONTROLLER_LANE = Lane(automation=False)
WORKER_LANE = Lane(automation=True)


def _claim(lane: Lane) -> Run:
    def run(parameters: Parameters, payload: Any) -> Any:
        # Every applying pass claims, an idle one included, before any work
        # that could end it early, and neither a preflight nor a glance pass
        # does: so a claim on the controller's socket is the controller
        # arriving. A glance pass runs when a person asks, and counted, it
        # would hide a controller that had stopped. One on the worker's socket
        # is HQ's playbook worker arriving instead.
        del payload
        capabilities = _capabilities(parameters)
        refused = next((kind for kind, _action in capabilities if not lane.holds(kind)), None)
        if refused is not None:
            raise ValueError(f"This socket claims no {refused} work.")
        if lane.automation:
            note_automation()
        else:
            note_controller()
        return claim_next_operation(
            parameters["controller-id"],
            lease_seconds=parameters["lease-seconds"],
            capabilities=capabilities,
            attempt=parameters["attempt"] or "",
        )

    return run


def _renew(lane: Lane) -> Run:
    def run(parameters: Parameters, payload: Any) -> Any:
        # Held to the contract's ``RunProgress`` before it arrives here.
        return renew_operation(
            lane.operation(parameters["operation"]),
            controller_id=parameters["controller-id"],
            lease_seconds=parameters["lease-seconds"],
            events=payload.get("events"),
        )

    return run


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


@final
class Admitted(Sealed):
    """A controller the bridge admitted, the only thing a controller report's writer takes (``sealing``).

    A report's writer asks nobody's permission: the bridge decided at
    admission. Only ``_reports`` holds the seal, so no other door has a value
    to hand a writer, and the controller each row is stored against comes from
    here, never from the caller.
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Admission:
    """What an ``Admitted`` carries: the controller, and HQ itself as who
    records its report, since nobody acts on the controller's behalf."""

    controller_id: str
    principal: Principal


def _reports(*writers: Callable[..., Any]) -> tuple[tuple[Run, ...], Callable[[Admitted], Admission]]:
    """Each report's action, handing its writer the bridge's admission, and the unsealer that reads one."""

    seal, unseal = sealer(Admitted, Admission)

    def recorded(record: Callable[..., Any]) -> Run:
        def run(parameters: Parameters, payload: Any) -> Any:
            admitted = seal(Admission(parameters["controller-id"], internal_principal(CONTROLLER)))
            return record(payload, admitted=admitted)

        return run

    return tuple(recorded(writer) for writer in writers), unseal


(_sweep, _connections, _steps, _analytics, _glance), admission = _reports(
    record_sweep, record_connections, record_step_failures, record_analytics, record_dashboard_observations
)


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
    return run_for_host(parameters["name"])


def _events(parameters: Parameters, payload: Any) -> Any:
    # Held to the contract's ``InboundBatch`` before it arrives here.
    del parameters
    return apply_events(payload)


def _schedule(parameters: Parameters, payload: Any) -> Any:
    del payload
    return schedule_automatic_operations(parameters["controller-id"])


def _report(lane: Lane) -> Run:
    def run(parameters: Parameters, payload: Any) -> Any:
        # Held to the contract's ``ControllerReport`` before it arrives here.
        return report_operation(
            lane.operation(parameters["operation"]),
            ControllerReport(**payload),
            controller_id=parameters["controller-id"],
        )

    return run


@dataclass(frozen=True, slots=True)
class Action:
    """One thing the controller may ask, and what a person calls its asking:
    the title of every audit row written while it runs (``audit_words.title_of``)."""

    name: str
    title: str
    run: Run


def _queue(lane: Lane) -> tuple[Action, ...]:
    """The three actions that lease queued work and end it, held to ``lane``."""

    return (
        Action("claim", "The controller took the next queued work", _claim(lane)),
        Action("renew", "A worker said how far it is on queued work", _renew(lane)),
        Action("report", "The controller said how queued work ended", _report(lane)),
    )


# The controller's socket: the whole contract.
ACTIONS: tuple[Action, ...] = (
    *_queue(CONTROLLER_LANE),
    Action("peek", "The controller looked for queued work", _peek),
    Action("export", "The controller read what a record declares", _export),
    Action("schedule", "The controller queued the automatic work", _schedule),
    Action("material", "The controller fetched a certificate's key", _material),
    Action(READINGS_ACTION, "The controller read what exists", _sweep),
    Action("connections", "The controller said what it can reach", _connections),
    Action("steps", "The controller said what it could not finish", _steps),
    Action("analytics", "The controller sent site analytics", _analytics),
    Action("analytics-plan", "The controller asked which analytics to read", _analytics_plan),
    Action("sweep-due", "The controller asked which readings are due", _sweep_due),
    Action("glance-plan", "The controller asked which panels to refresh", _glance_plan),
    Action("glance", "The controller refreshed the dashboard", _glance),
    Action("registry", "The controller read what each provider declares", _registry),
    Action("job", "The controller ran scheduled work", _job),
    Action("events", "The listener said what happened outside", _events),
)
# HQ's playbook worker's socket: its claim, renewal and report, and nothing else.
WORKER_ACTIONS: tuple[Action, ...] = _queue(WORKER_LANE)
