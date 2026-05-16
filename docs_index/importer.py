"""
Documentation manifest importer.

Reads a JSON array like:

    [{
        "doc_id": "rb-adguard-001",
        "title": "AdGuard Home Runbook",
        "path": "Infra/DNS/AdGuard Home.md",
        "doc_type": "runbook",
        "system": "AdGuard Home",
        "environment": "homelab",
        "status": "active",
        "sensitivity": "sensitive",
        "related_projects": ["homelab-dns"],
        "related_assets": ["optiplex-7050"],
        "last_reviewed": "2026-05-16"
    }, ...]

and upserts DocumentationRecord rows. The Obsidian vault stays the source of
truth — we only track metadata + relationships.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from django.db import transaction

from assets.models import Asset
from projects.models import Project

from .models import DocumentationRecord


class ManifestImportError(ValueError):
    pass


def _coerce_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError as exc:
        raise ManifestImportError(f"Invalid date {value!r}: {exc}") from exc


def _validate_choice(value: str, choices, *, field: str) -> str:
    allowed = {v for v, _ in choices}
    if value in allowed:
        return value
    raise ManifestImportError(
        f"Invalid {field}={value!r}. Allowed: {sorted(allowed)}"
    )


@transaction.atomic
def import_manifest_data(
    items: Iterable[dict],
    *,
    update_existing: bool = True,
) -> dict[str, int]:
    if not isinstance(items, list):
        raise ManifestImportError("Manifest must be a JSON array of records.")

    stats = {"created": 0, "updated": 0, "skipped": 0, "missing_relations": 0}

    for entry in items:
        doc_id = (entry.get("doc_id") or "").strip()
        if not doc_id:
            stats["skipped"] += 1
            continue

        defaults = {
            "title": entry.get("title") or doc_id,
            "doc_type": _validate_choice(
                entry.get("doc_type") or "runbook",
                DocumentationRecord.DocType.choices,
                field="doc_type",
            ),
            "system_service": entry.get("system") or entry.get("system_service") or "",
            "environment": _validate_choice(
                entry.get("environment") or "other",
                DocumentationRecord.Environment.choices,
                field="environment",
            ),
            "status": _validate_choice(
                entry.get("status") or "draft",
                DocumentationRecord.Status.choices,
                field="status",
            ),
            "sensitivity": _validate_choice(
                entry.get("sensitivity") or "internal",
                DocumentationRecord.Sensitivity.choices,
                field="sensitivity",
            ),
            "obsidian_path": entry.get("path") or entry.get("obsidian_path") or "",
            "github_path": entry.get("github_path") or "",
            "external_url": entry.get("external_url") or "",
            "last_reviewed": _coerce_date(entry.get("last_reviewed")),
            "notes": entry.get("notes") or "",
        }

        record, created = DocumentationRecord.objects.get_or_create(
            doc_id=doc_id, defaults=defaults
        )
        if created:
            stats["created"] += 1
        elif update_existing:
            for k, v in defaults.items():
                setattr(record, k, v)
            record.save()
            stats["updated"] += 1
        else:
            stats["skipped"] += 1
            continue

        project_slugs = entry.get("related_projects") or []
        asset_slugs = entry.get("related_assets") or []

        if project_slugs:
            qs = Project.objects.filter(slug__in=project_slugs)
            record.related_projects.set(qs)
            stats["missing_relations"] += len(project_slugs) - qs.count()
        if asset_slugs:
            qs = Asset.objects.filter(slug__in=asset_slugs)
            record.related_assets.set(qs)
            stats["missing_relations"] += len(asset_slugs) - qs.count()

    return stats
