"""Read-only registry diagnostics shared by every delivery adapter."""

from __future__ import annotations

from typing import Any

from django.db.models import Count

from assets.models import Asset
from projects.models import Project


def audit_registry() -> dict[str, Any]:
    """Return registry rows that no documentation record references."""

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
    return {
        "ok": True,
        "projects_total": Project.objects.count(),
        "assets_total": Asset.objects.count(),
        "orphan_projects": orphan_projects,
        "orphan_assets": orphan_assets,
    }
