"""Describe or execute HQ's canonical JSON capabilities."""

import json
import sys

from django.core.management.base import BaseCommand, CommandError

from application.capabilities import describe_capabilities, execute_capability
from application.security import cli_principal


class Command(BaseCommand):
    help = "Describe or execute a canonical HQ JSON capability."

    def add_arguments(self, parser):
        subcommands = parser.add_subparsers(dest="action", required=True)
        subcommands.add_parser("describe")
        run = subcommands.add_parser("run")
        run.add_argument("name")
        run.add_argument(
            "--payload",
            default="-",
            help="JSON object or '-' to read JSON from stdin.",
        )
        run.add_argument("--target")
        run.add_argument("--expected-updated-at")

    def handle(self, *args, **options):
        if options["action"] == "describe":
            self.stdout.write(json.dumps(describe_capabilities(), sort_keys=True))
            return
        raw = sys.stdin.read() if options["payload"] == "-" else options["payload"]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise CommandError("Payload must be a JSON object.")
        result = execute_capability(
            options["name"],
            payload,
            principal=cli_principal(),
            target=options["target"],
            expected_updated_at=options["expected_updated_at"],
        )
        self.stdout.write(json.dumps(result, sort_keys=True))
        if not result["ok"]:
            raise CommandError(result["error"]["message"])
