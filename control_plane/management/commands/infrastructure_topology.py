"""Import and inspect the trusted Severino Labs topology snapshot."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from django.core.management.base import BaseCommand, CommandError

from control_plane.topology import TopologyError, import_topology


class Command(BaseCommand):
    help = "Import a validated topology v2 JSON document into HQ."

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
            snapshot = import_topology(payload)
        except (OSError, json.JSONDecodeError, TopologyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "schema_version": snapshot.schema_version,
                    "checksum": snapshot.checksum,
                },
                sort_keys=True,
            )
        )
