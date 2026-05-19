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
        parser.add_argument(
            "--report-orphans",
            action="store_true",
            help="After import, list doc_ids that exist in HQ but not in the manifest.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help=(
                "Delete orphan DocumentationRecord rows (doc_ids in HQ but not in "
                "the manifest). Implies --report-orphans. Use after a doc_id rename "
                "or doc retirement. ContentItems are not touched — they re-key by "
                "slug and get re-linked by the manifest's new record."
            ),
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

        report_orphans = options["report_orphans"] or options["prune"]

        try:
            stats = import_manifest_data(
                data,
                update_existing=not options["no_update"],
                report_orphans=report_orphans,
                prune_orphans=options["prune"],
            )
        except ManifestImportError as exc:
            raise CommandError(str(exc)) from exc

        summary = {
            k: v for k, v in stats.items()
            if k not in {"missing_relations_detail", "orphans"}
        }
        record_event(
            action=AuditLog.Action.IMPORTED,
            type_label="DocumentationRecord",
            message=f"CLI manifest import: {summary}",
            metadata=stats,
        )

        self.stdout.write(self.style.SUCCESS(f"Manifest imported: {summary}"))

        for entry in stats.get("missing_relations_detail", []):
            self.stdout.write(self.style.WARNING(
                f"  missing {entry['kind']}: {entry['doc_id']} → {entry['slug']}"
            ))

        if report_orphans:
            orphans = stats.get("orphans", [])
            if orphans:
                verb = "pruned" if options["prune"] else "found"
                self.stdout.write(self.style.WARNING(
                    f"Orphans {verb} ({len(orphans)} HQ rows with no manifest entry):"
                ))
                for doc_id in orphans:
                    self.stdout.write(self.style.WARNING(f"  orphan: {doc_id}"))
                if options["prune"]:
                    self.stdout.write(self.style.SUCCESS(
                        f"Deleted {stats.get('orphans_pruned_records', 0)} row(s) "
                        f"({stats['orphans_pruned']} DocumentationRecord + cascades)."
                    ))
            else:
                self.stdout.write("No orphans.")
