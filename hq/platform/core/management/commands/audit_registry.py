"""Audit the Project/Asset registries against documentation references.

Read-only, and the inverse of ``import_docs_manifest --report-orphans``: it
reports registry rows no documentation references, the fingerprint of a rename
that left a stale slug or of a duplicate row (or a project with no doc yet).
"""

import json
from typing import override

from django.core.management.base import BaseCommand

from hq.platform.application.registry import audit_registry


class Command(BaseCommand):
    help = "Report Project/Asset registry rows that no documentation references."

    @override
    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit raw JSON for wrapper CLIs instead of the human report.",
        )

    @override
    def handle(self, *args, **options):
        stats = audit_registry()
        orphan_projects = stats["orphan_projects"]
        orphan_assets = stats["orphan_assets"]

        if options["json"]:
            self.stdout.write(json.dumps(stats, default=str))
            return

        self.stdout.write(f"Projects  {stats['projects_total']} total, {len(orphan_projects)} with zero docs")
        for slug in orphan_projects:
            self.stdout.write(f"          orphan: {slug}")
        self.stdout.write(f"Assets    {stats['assets_total']} total, {len(orphan_assets)} with zero docs")
        for slug in orphan_assets:
            self.stdout.write(f"          orphan: {slug}")

        if orphan_projects or orphan_assets:
            self.stdout.write("Registry  review orphans above: a stale slug from a rename, or a duplicate row")
        else:
            self.stdout.write("Registry  ok: every Project and Asset is referenced by at least one doc")
