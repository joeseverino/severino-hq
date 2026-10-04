"""Machine-readable bridge used by the privileged homelab controller.

Every action is declared once, in ``ACTIONS``: its name, the flags it takes,
and the function that runs it. The subparsers and the dispatch are both derived
from that, so an action cannot be accepted by the parser and missing from the
dispatch, or fall through to another action's handler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from pydantic import TypeAdapter, ValidationError

from hq.platform.application.controller import (
    ControllerReport,
    claim_next_operation,
    controller_registry,
    peek_next_operation,
    report_operation,
    schedule_automatic_operations,
)
from hq.platform.application.certificates import CertificateError, material_for
from hq.platform.application.cadence import note_controller, sweep_due
from hq.platform.application.analytics import analytics_plan, record_analytics
from hq.platform.application.inventory import record_connections, record_step_failures
from hq.platform.application.sweep import record_sweep
from hq.platform.application.security import cli_principal
from hq.platform.application.infrastructure import controller_contract
from hq.platform.application.glance import dashboard_refresh_plan, record_dashboard_observations
from hq.domains.control_plane.models import ManagedResource


# Flags shared across actions, declared once so "--controller-id" means the
# same thing wherever it is accepted.
FLAGS: dict[str, Callable[[Any], None]] = {
    "controller_id": lambda parser: parser.add_argument(
        "--controller-id", required=True
    ),
    "lease_seconds": lambda parser: parser.add_argument(
        "--lease-seconds", type=int, default=300
    ),
    "capability": lambda parser: parser.add_argument(
        "--capability", action="append", default=[]
    ),
    "resource": lambda parser: parser.add_argument("--resource", required=True),
    # "-" reads it from standard input, which is how the controller sends it:
    # one argument is capped at 128 KiB, and a whole sweep is larger.
    "payload": lambda parser: parser.add_argument("--payload", required=True),
    "operation": lambda parser: parser.add_argument("--operation", required=True),
}


def _capabilities(options: dict) -> tuple[tuple[str, str], ...]:
    parsed = tuple(
        tuple(value.split(":", 1)) for value in options.get("capability", [])
    )
    if any(len(item) != 2 or not all(item) for item in parsed):
        raise ValueError("Capabilities must use kind:action.")
    return parsed


def _claim(options: dict) -> Any:
    return claim_next_operation(
        options["controller_id"],
        lease_seconds=options["lease_seconds"],
        capabilities=_capabilities(options),
    )


def _peek(options: dict) -> Any:
    return peek_next_operation(capabilities=_capabilities(options))


def _export(options: dict) -> Any:
    try:
        resource = ManagedResource.objects.get(key=options["resource"])
    except ManagedResource.DoesNotExist as exc:
        raise ValueError("Managed resource was not found.") from exc
    return controller_contract(resource)


def _material(options: dict) -> Any:
    # Its own action rather than a field on the contract: `export` prints a
    # contract, and a stored private key must not be one keystroke away from a
    # terminal that was only being inspected.
    try:
        return material_for(options["resource"])
    except CertificateError as exc:
        raise ValueError(str(exc)) from exc


def _recorded(record: Callable[..., Any]) -> Callable[[dict], Any]:
    """A controller's report, parsed and recorded against it as the CLI principal."""

    def run(options: dict) -> Any:
        return record(
            json.loads(options["payload"]),
            principal=cli_principal(),
            controller_id=options["controller_id"],
        )

    return run


def _analytics_plan(options: dict) -> Any:
    return analytics_plan(json.loads(options["payload"]))


def _sweep_due(options: dict) -> Any:
    return sweep_due(options["controller_id"])


def _glance_plan(options: dict) -> Any:
    # The first call of every applying run, before any work that could end it
    # early, and one a preflight never makes: so this is the controller
    # arriving, noted before anything it does can go wrong.
    note_controller()
    return dashboard_refresh_plan(options["controller_id"])


def _registry(options: dict) -> Any:
    del options
    return controller_registry()


def _schedule(options: dict) -> Any:
    return schedule_automatic_operations(options["controller_id"])


def _report(options: dict) -> Any:
    payload = json.loads(options["payload"])
    parsed = TypeAdapter(ControllerReport).validate_python(payload)
    return report_operation(
        options["operation"], parsed, controller_id=options["controller_id"]
    )


@dataclass(frozen=True)
class Action:
    name: str
    flags: tuple[str, ...]
    run: Callable[[dict], Any]


ACTIONS: tuple[Action, ...] = (
    Action("claim", ("controller_id", "lease_seconds", "capability"), _claim),
    Action("peek", ("capability",), _peek),
    Action("export", ("resource",), _export),
    Action("schedule", ("controller_id",), _schedule),
    Action("material", ("resource",), _material),
    Action("inventory", ("controller_id", "payload"), _recorded(record_sweep)),
    Action("connections", ("controller_id", "payload"), _recorded(record_connections)),
    Action("steps", ("controller_id", "payload"), _recorded(record_step_failures)),
    Action("analytics", ("controller_id", "payload"), _recorded(record_analytics)),
    Action("analytics-plan", ("payload",), _analytics_plan),
    Action("sweep-due", ("controller_id",), _sweep_due),
    Action("glance-plan", ("controller_id",), _glance_plan),
    Action(
        "glance", ("controller_id", "payload"), _recorded(record_dashboard_observations)
    ),
    Action("report", ("controller_id", "operation", "payload"), _report),
    Action("registry", (), _registry),
)

BY_NAME = {action.name: action for action in ACTIONS}


class Command(BaseCommand):
    help = "Claim or report a typed infrastructure operation as JSON."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        for action in ACTIONS:
            subparser = subparsers.add_parser(action.name)
            for flag in action.flags:
                FLAGS[flag](subparser)

    def handle(self, *args, **options):
        # No fallback branch. argparse only admits a name that is in ACTIONS,
        # and ACTIONS is what built the parser, so the two cannot drift apart.
        if options.get("payload") == "-":
            options["payload"] = sys.stdin.read()
        try:
            result = BY_NAME[options["action"]].run(options)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
