"""Read-only registry diagnostics shared by every delivery adapter."""

from typing import TypedDict

from django.db.models import Count

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project

from .security import Capability, Principal


class RegistryAudit(TypedDict):
    """The Project and Asset records no documentation points to, by slug, beside how many there are."""

    projects_total: int
    assets_total: int
    orphan_projects: list[str]
    orphan_assets: list[str]


def audit_registry(*, principal: Principal) -> RegistryAudit:
    """Return registry rows that no documentation record references."""

    principal.require(Capability.READ)

    orphan_projects = sorted(
        Project.objects.annotate(_docs=Count("documentation_records")).filter(_docs=0).values_list("slug", flat=True)
    )
    orphan_assets = sorted(
        Asset.objects.annotate(_docs=Count("documentation_records")).filter(_docs=0).values_list("slug", flat=True)
    )
    return {
        "projects_total": Project.objects.count(),
        "assets_total": Asset.objects.count(),
        "orphan_projects": orphan_projects,
        "orphan_assets": orphan_assets,
    }
