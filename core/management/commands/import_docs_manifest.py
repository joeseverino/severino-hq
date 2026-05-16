"""Import a documentation manifest from a JSON file or stdin.

Examples:

    python manage.py import_docs_manifest path/to/docs_manifest.json
    cat docs_manifest.json | python manage.py import_docs_manifest -

The manifest is the same JSON shape the web import accepts. See
``docs_index/importer.py`` for the schema.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.audit import record_event
from core.models import AuditLog
from docs_index.importer import ManifestImportError, import_manifest_data


class Command(BaseCommand):
    help = "Import a documentation manifest JSON file into the docs index."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            help="Path to JSON manifest file, or '-' to read from stdin.",
        )
        parser.add_argument(
            "--no-update",
            action="store_true",
            help="Do not update existing doc_id records — only create new ones.",
        )

    def handle(self, *args, **options):
        path = options["path"]
        if path == "-":
            raw = sys.stdin.read()
        else:
            file_path = Path(path)
            if not file_path.is_file():
                raise CommandError(f"Manifest file not found: {file_path}")
            raw = file_path.read_text(encoding="utf-8")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON: {exc}") from exc

        try:
            stats = import_manifest_data(
                data,
                update_existing=not options["no_update"],
            )
        except ManifestImportError as exc:
            raise CommandError(str(exc)) from exc

        record_event(
            action=AuditLog.Action.IMPORTED,
            type_label="DocumentationRecord",
            message=f"CLI manifest import: {stats}",
            metadata=stats,
        )

        self.stdout.write(self.style.SUCCESS(f"Manifest imported: {stats}"))
