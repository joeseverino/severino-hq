"""Import one JSON document of projects and assets.

    python manage.py import_registry registry.json
    cat registry.json | python manage.py import_registry - --check-only

The document is ``{"projects": [...], "assets": [...]}``; each record takes the
fields of ``project.upsert`` or ``asset.upsert`` and needs a slug. Runs the
``hq.import`` capability.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from application.capabilities import execute_capability
from application.security import cli_principal
from application.ui import counted


class Command(BaseCommand):
    help = "Import projects and assets from one JSON document, all or nothing."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the JSON document, or '-' for stdin.")
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="Validate and run the import, then roll it back.",
        )
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def _document(self, path: str) -> dict:
        if path == "-":
            raw = sys.stdin.read()
        else:
            file_path = Path(path)
            if not file_path.is_file():
                raise CommandError(f"Document not found: {file_path}")
            raw = file_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CommandError('The document is an object: {"projects": [...], "assets": [...]}.')
        return data

    def handle(self, *args, **options):
        payload = {**self._document(options["path"]), "check_only": options["check_only"]}
        result = execute_capability("hq.import", payload, principal=cli_principal())
        if options["json"]:
            self.stdout.write(json.dumps(result, default=str))
        if not result["ok"]:
            if not options["json"]:
                for problem in result.get("problems", ()):
                    label = f"{problem['kind']} {problem['slug'] or '#' + str(problem['index'])}"
                    for error in problem["errors"]:
                        self.stderr.write(f"  {label}: {error}")
            error = result.get("error", {})
            raise CommandError(
                error.get("message")
                or f"{counted(len(result.get('problems', ())), 'record')} refused. Nothing was imported."
            )
        if options["json"]:
            return
        for kind in ("projects", "assets"):
            for item in result[kind]:
                for kept in item["kept"]:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  {item['slug']}: kept {kept['field']} {kept['kept']}; "
                            f"the document offered {kept['offered']}."
                        )
                    )
        summary = result["summary"]
        verb = "Would import" if result["check_only"] else "Imported"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {counted(len(result['projects']), 'project')} and "
                f"{counted(len(result['assets']), 'asset')}: "
                f"{summary['created']} created, {summary['updated']} updated, "
                f"{summary['unchanged']} unchanged."
            )
        )
