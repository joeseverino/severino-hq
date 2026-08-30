"""Machine-readable bridge used by the privileged homelab controller.

Every action is declared once, in ``ACTIONS``: its name, the flags it takes,
and the function that runs it. The subparsers and the dispatch are both derived
from that.

Stated twice -- a subparser here, a branch in an if/elif ladder there -- the two
could disagree, and one disagreement was dangerous rather than merely untidy:
the ladder ended in a bare ``else`` that ran ``report``, so an action added to
the parser and forgotten in the ladder would not fail. It would quietly report
an operation instead.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from pydantic import TypeAdapter, ValidationError

from application.controller import (
    ControllerReport,
    claim_next_operation,
    peek_next_operation,
    report_operation,
    schedule_automatic_operations,
)
from application.certificates import CertificateError, material_for
from application.cadence import sweep_due
from application.analytics import analytics_plan, record_analytics
from application.inventory import record_connections
from application.sweep import record_sweep
from application.security import cli_principal
from application.infrastructure import controller_contract
from application.glance import dashboard_refresh_plan, record_dashboard_observations
from control_plane.models import ManagedResource


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


def _inventory(options: dict) -> Any:
    return record_sweep(
        json.loads(options["payload"]),
        principal=cli_principal(),
        controller_id=options["controller_id"],
    )


def _connections(options: dict) -> Any:
    return record_connections(
        json.loads(options["payload"]),
        principal=cli_principal(),
        controller_id=options["controller_id"],
    )


def _analytics(options: dict) -> Any:
    return record_analytics(
        json.loads(options["payload"]),
        principal=cli_principal(),
        controller_id=options["controller_id"],
    )


def _analytics_plan(options: dict) -> Any:
    return analytics_plan(json.loads(options["payload"]))


def _sweep_due(options: dict) -> Any:
    del options
    return sweep_due()


def _glance_plan(options: dict) -> Any:
    return dashboard_refresh_plan(options["controller_id"])


def _glance(options: dict) -> Any:
    payload = json.loads(options["payload"])
    return record_dashboard_observations(
        payload,
        principal=cli_principal(),
        controller_id=options["controller_id"],
    )


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
    Action("inventory", ("controller_id", "payload"), _inventory),
    Action("connections", ("controller_id", "payload"), _connections),
    Action("analytics", ("controller_id", "payload"), _analytics),
    Action("analytics-plan", ("payload",), _analytics_plan),
    Action("sweep-due", (), _sweep_due),
    Action("glance-plan", ("controller_id",), _glance_plan),
    Action("glance", ("controller_id", "payload"), _glance),
    Action("report", ("controller_id", "operation", "payload"), _report),
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
        try:
            result = BY_NAME[options["action"]].run(options)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
