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

from application.documentation import sync_documentation
from application.security import cli_principal
from docs_index.importer import (
    ManifestImportError,
    validate_manifest_data,
)


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
                "or doc retirement. Mirrored ContentItems that are only linked to "
                "pruned docs are removed with them."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print raw import stats as JSON for wrapper CLIs.",
        )
        parser.add_argument(
            "--check-only",
            action="store_true",
            help=(
                "Validate the manifest against the canonical schema and exit "
                "without touching the database. Exit 1 if any entry has an "
                "invalid enum (status/doc_type/environment/sensitivity). Run it "
                "locally as an `hq sync` preflight so contract drift fails on the "
                "dev machine, not as a prod round-trip."
            ),
        )

    # Read, run, render -- kept apart because they fail differently and change
    # for different reasons. Interleaved, the two output formats were stated in
    # two places each and the reporting drowned the control flow.

    def _manifest(self, path: str):
        """The manifest as data, from a file or stdin."""

        if path == "-":
            raw = sys.stdin.read()
        else:
            file_path = Path(path)
            if not file_path.is_file():
                raise CommandError(f"Manifest file not found: {file_path}")
            raw = file_path.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON: {exc}") from exc

    def _check_only(self, data, *, as_json: bool) -> None:
        problems = validate_manifest_data(data)
        if as_json:
            self.stdout.write(json.dumps({"ok": not problems, "problems": problems}))
        elif problems:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(problems)} manifest entr(ies) would be rejected:"
                )
            )
            for problem in problems:
                label = problem["doc_id"] or "(no doc_id)"
                for error in problem["errors"]:
                    self.stdout.write(self.style.ERROR(f"  {label}: {error}"))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Manifest valid: {len(data)} entr(ies) pass schema validation."
                )
            )
        if problems:
            raise CommandError(
                f"{len(problems)} invalid manifest entr(ies) — not importable."
            )

    def _import(self, data, *, options, report_orphans: bool):
        try:
            result = sync_documentation(
                data,
                principal=cli_principal(),
                update_existing=not options["no_update"],
                report_orphans=report_orphans,
                prune_orphans=options["prune"],
                confirm_prune=options["prune"],
            )
        except ManifestImportError as exc:
            raise CommandError(str(exc)) from exc
        if not result["ok"]:
            raise CommandError(f"Manifest validation failed: {result['problems']}")
        return result["stats"]

    def _report_orphans(self, stats, *, pruned: bool) -> None:
        orphans = stats.get("orphans", [])
        if not orphans:
            self.stdout.write("No orphans.")
            return
        verb = "pruned" if pruned else "found"
        self.stdout.write(
            self.style.WARNING(
                f"Orphans {verb} ({len(orphans)} HQ rows with no manifest entry):"
            )
        )
        for doc_id in orphans:
            self.stdout.write(self.style.WARNING(f"  orphan: {doc_id}"))
        if pruned:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {stats.get('orphans_pruned_records', 0)} row(s) "
                    f"({stats['orphans_pruned']} DocumentationRecord + cascades)."
                )
            )

    def _render(self, stats, *, options, report_orphans: bool) -> None:
        summary = {
            key: value
            for key, value in stats.items()
            if key not in {"missing_relations_detail", "orphans"}
        }
        self.stdout.write(self.style.SUCCESS(f"Manifest imported: {summary}"))
        for entry in stats.get("missing_relations_detail", []):
            self.stdout.write(
                self.style.WARNING(
                    f"  missing {entry['kind']}: {entry['doc_id']} → {entry['slug']}"
                )
            )
        if report_orphans:
            self._report_orphans(stats, pruned=options["prune"])
        if stats.get("content_items_pruned"):
            self.stdout.write(
                self.style.SUCCESS(
                    f"Pruned {stats['content_items_pruned']} stale mirrored "
                    "ContentItem row(s)."
                )
            )

    def handle(self, *args, **options):
        data = self._manifest(options["path"])
        if options["check_only"]:
            self._check_only(data, as_json=options["json"])
            return

        # --prune implies --report-orphans: pruning without listing what went
        # would be a deletion with no record of it in the output.
        report_orphans = options["report_orphans"] or options["prune"]
        stats = self._import(data, options=options, report_orphans=report_orphans)
        if options["json"]:
            self.stdout.write(json.dumps(stats, default=str))
            return
        self._render(stats, options=options, report_orphans=report_orphans)
