"""Import and inspect the trusted Severino Labs topology snapshot."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from django.core.management.base import BaseCommand, CommandError

from application.security import cli_principal
from application.topology import sync_topology
from control_plane.topology import TopologyError


class Command(BaseCommand):
    help = "Import topology v3 and materialize its managed resource declarations."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            nargs="?",
            default="-",
            help="Validated topology JSON path, or -/omitted for stdin.",
        )

    def handle(self, *args, **options):
        try:
            source = options["path"]
            text = (
                sys.stdin.read()
                if source == "-"
                else Path(source).read_text(encoding="utf-8")
            )
            payload = json.loads(text)
            result = sync_topology(payload, principal=cli_principal())
        except (OSError, json.JSONDecodeError, TopologyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
