"""Machine-readable bridge used by the privileged homelab controller."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from pydantic import TypeAdapter, ValidationError

from application.controller import (
    ControllerReport,
    claim_next_operation,
    peek_next_operation,
    report_operation,
    schedule_automatic_operations,
)
from application.connections import preflight_connections
from application.certificates import CertificateError, material_for
from application.inventory import record_inventory
from application.security import cli_principal
from application.infrastructure import controller_contract
from control_plane.models import ManagedResource


class Command(BaseCommand):
    help = "Claim or report a typed infrastructure operation as JSON."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        claim = subparsers.add_parser("claim")
        claim.add_argument("--controller-id", required=True)
        claim.add_argument("--lease-seconds", type=int, default=300)
        claim.add_argument("--capability", action="append", default=[])
        peek = subparsers.add_parser("peek")
        peek.add_argument("--capability", action="append", default=[])

        export = subparsers.add_parser("export")
        export.add_argument("--resource", required=True)
        subparsers.add_parser("preflight")
        schedule = subparsers.add_parser("schedule")
        schedule.add_argument("--controller-id", required=True)

        material = subparsers.add_parser("material")
        material.add_argument("--resource", required=True)

        inventory = subparsers.add_parser("inventory")
        inventory.add_argument("--controller-id", required=True)
        inventory.add_argument("--payload", required=True)

        report = subparsers.add_parser("report")
        report.add_argument("--controller-id", required=True)
        report.add_argument("--operation", required=True)
        report.add_argument("--payload", required=True)

    def handle(self, *args, **options):
        try:
            capabilities = tuple(
                tuple(value.split(":", 1)) for value in options.get("capability", [])
            )
            if any(len(item) != 2 or not all(item) for item in capabilities):
                raise ValueError("Capabilities must use kind:action.")
            if options["action"] == "claim":
                result = claim_next_operation(
                    options["controller_id"],
                    lease_seconds=options["lease_seconds"],
                    capabilities=capabilities,
                )
            elif options["action"] == "peek":
                result = peek_next_operation(capabilities=capabilities)
            elif options["action"] == "export":
                try:
                    resource = ManagedResource.objects.get(key=options["resource"])
                except ManagedResource.DoesNotExist as exc:
                    raise ValueError("Managed resource was not found.") from exc
                result = controller_contract(resource)
            elif options["action"] == "preflight":
                probes = preflight_connections()
                result = {
                    "ok": all(probe.ok for probe in probes),
                    "connections": [
                        {
                            "connection_ref": probe.connection_ref,
                            "provider": probe.provider,
                            "ok": probe.ok,
                            "message": probe.message,
                        }
                        for probe in probes
                    ],
                }
                if not result["ok"]:
                    raise ValueError(json.dumps(result, sort_keys=True))
            elif options["action"] == "material":
                # Its own command rather than a field on the contract: `export`
                # prints a contract, and a stored private key must not be one
                # keystroke away from a terminal that was only being inspected.
                try:
                    result = material_for(options["resource"])
                except CertificateError as exc:
                    raise ValueError(str(exc)) from exc
            elif options["action"] == "inventory":
                result = record_inventory(
                    json.loads(options["payload"]),
                    principal=cli_principal(),
                    controller_id=options["controller_id"],
                )
            elif options["action"] == "schedule":
                result = schedule_automatic_operations(options["controller_id"])
            else:
                payload = json.loads(options["payload"])
                parsed = TypeAdapter(ControllerReport).validate_python(payload)
                result = report_operation(
                    options["operation"],
                    parsed,
                    controller_id=options["controller_id"],
                )
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
