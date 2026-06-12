"""Audit the Project/Asset registries against documentation references.

The inverse of ``import_docs_manifest --report-orphans``: that reports vault
relations pointing at a missing registry slug; this reports registry rows that
*no* documentation references (zero ``documentation_records``). A zero-doc row
is not always wrong — a freshly registered project may not have a doc yet — but
it is the fingerprint of a rename that left a stale slug, or a duplicate row.

Read-only. Replaces an inline ORM script that `hq validate` used to pipe into
`manage.py shell` over SSH, so the logic is now testable and versioned.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from assets.models import Asset
from projects.models import Project


class Command(BaseCommand):
    help = "Report Project/Asset registry rows that no documentation references."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit raw JSON for wrapper CLIs instead of the human report.",
        )

    def handle(self, *args, **options):
        orphan_projects = sorted(
            Project.objects.annotate(_docs=Count("documentation_records"))
            .filter(_docs=0)
            .values_list("slug", flat=True)
        )
        orphan_assets = sorted(
            Asset.objects.annotate(_docs=Count("documentation_records"))
            .filter(_docs=0)
            .values_list("slug", flat=True)
        )
        stats = {
            "projects_total": Project.objects.count(),
            "assets_total": Asset.objects.count(),
            "orphan_projects": orphan_projects,
            "orphan_assets": orphan_assets,
        }

        if options["json"]:
            self.stdout.write(json.dumps(stats, default=str))
            return

        self.stdout.write(
            "Projects  %d total, %d with zero docs"
            % (stats["projects_total"], len(orphan_projects))
        )
        for slug in orphan_projects:
            self.stdout.write(f"          orphan: {slug}")
        self.stdout.write(
            "Assets    %d total, %d with zero docs"
            % (stats["assets_total"], len(orphan_assets))
        )
        for slug in orphan_assets:
            self.stdout.write(f"          orphan: {slug}")

        if orphan_projects or orphan_assets:
            self.stdout.write(
                "Registry  review orphans above — stale slug from a rename, "
                "or a duplicate row"
            )
        else:
            self.stdout.write(
                "Registry  ok — every Project and Asset is referenced by at "
                "least one doc"
            )
