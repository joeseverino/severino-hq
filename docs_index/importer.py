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
from content.models import ContentItem
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


# Map vault `content_type` frontmatter values to ContentItem.Type.
CONTENT_TYPE_MAP = {
    "portfolio_article": ContentItem.Type.PORTFOLIO_PAGE,
    "blog_post": ContentItem.Type.ARTICLE,
    "lab_writeup": ContentItem.Type.LAB_WRITEUP,
    "case_study": ContentItem.Type.CASE_STUDY,
    "guide": ContentItem.Type.GUIDE,
    "review": ContentItem.Type.REVIEW,
    "video": ContentItem.Type.VIDEO,
    "service_page": ContentItem.Type.SERVICE_PAGE,
    "page": ContentItem.Type.PAGE,
}


def _upsert_content_item(
    record: "DocumentationRecord",
    entry: dict,
    defaults: dict,
    stats: dict,
) -> None:
    """Mirror a public-article DocumentationRecord into ContentItem.

    Keyed by slug derived from `doc_id` (e.g. `article-foo` -> `foo`). Idempotent:
    if a ContentItem with that slug already exists, its fields are refreshed.
    """
    doc_id = record.doc_id
    # Strip the prefix so the ContentItem slug matches the URL slug on
    # jseverino.com (writeups) or the vault folder (pages, legacy articles).
    article_slug = doc_id
    for prefix in ("writeup-", "page-", "article-"):
        if article_slug.startswith(prefix):
            article_slug = article_slug.removeprefix(prefix)
            break

    ct_raw = (entry.get("content_type") or "").strip().lower()
    content_type = CONTENT_TYPE_MAP.get(ct_raw, ContentItem.Type.PORTFOLIO_PAGE)

    # `published` is the canonical gate. Older records without it fall back
    # to legacy publish markers.
    if "published" in entry:
        is_published = bool(entry.get("published"))
    else:
        is_published = bool(
            defaults.get("published_at") or defaults.get("external_url")
        )
    item_status = (
        ContentItem.Status.PUBLISHED if is_published else ContentItem.Status.DRAFT
    )

    tags = entry.get("tags") or entry.get("technologies") or []
    tags_str = ", ".join(t for t in tags if t) if isinstance(tags, list) else str(tags)

    content_defaults = {
        "title": defaults["title"],
        "content_type": content_type,
        "status": item_status,
        "topic": (
            entry.get("topic")
            or entry.get("description")
            or entry.get("excerpt")
            or entry.get("system")
            or ""
        ),
        "tags": tags_str,
        "published_url": defaults.get("external_url") or "",
        "published_at": defaults.get("published_at"),
    }

    item, created = ContentItem.objects.get_or_create(
        slug=article_slug, defaults=content_defaults
    )
    if not created:
        if any(getattr(item, k) != v for k, v in content_defaults.items()):
            for k, v in content_defaults.items():
                setattr(item, k, v)
            item.save()

    if not item.related_documentation.filter(pk=record.pk).exists():
        item.related_documentation.add(record)

    # Mirror the doc's project/asset relationships onto the ContentItem so
    # article cards and project pages cross-reference directly without a
    # join through DocumentationRecord.
    doc_project_ids = set(record.related_projects.values_list("pk", flat=True))
    if set(item.related_projects.values_list("pk", flat=True)) != doc_project_ids:
        item.related_projects.set(doc_project_ids)
    doc_asset_ids = set(record.related_assets.values_list("pk", flat=True))
    if set(item.related_assets.values_list("pk", flat=True)) != doc_asset_ids:
        item.related_assets.set(doc_asset_ids)

    stats.setdefault("content_items_synced", 0)
    stats["content_items_synced"] += 1


@transaction.atomic
def import_manifest_data(
    items: Iterable[dict],
    *,
    update_existing: bool = True,
    report_orphans: bool = False,
    prune_orphans: bool = False,
) -> dict[str, Any]:
    if not isinstance(items, list):
        raise ManifestImportError("Manifest must be a JSON array of records.")

    stats: dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "missing_relations": 0,
        "missing_relations_detail": [],
    }
    if report_orphans:
        stats["orphans"] = []

    manifest_doc_ids: set[str] = set()

    for entry in items:
        doc_id = (entry.get("doc_id") or "").strip()
        if not doc_id:
            stats["skipped"] += 1
            continue
        manifest_doc_ids.add(doc_id)

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
            "published_at": _coerce_date(entry.get("published_at")),
            "notes": entry.get("notes") or "",
        }

        record, created = DocumentationRecord.objects.get_or_create(
            doc_id=doc_id, defaults=defaults
        )
        if created:
            changed = True
        elif update_existing:
            changed = any(getattr(record, k) != v for k, v in defaults.items())
            if changed:
                for k, v in defaults.items():
                    setattr(record, k, v)
                record.save()
        else:
            stats["skipped"] += 1
            continue

        project_slugs = entry.get("related_projects") or []
        asset_slugs = entry.get("related_assets") or []

        if project_slugs:
            qs = Project.objects.filter(slug__in=project_slugs)
            desired = set(qs.values_list("pk", flat=True))
            current = set(record.related_projects.values_list("pk", flat=True))
            if desired != current:
                record.related_projects.set(qs)
                changed = True
            found_slugs = set(qs.values_list("slug", flat=True))
            for slug in project_slugs:
                if slug not in found_slugs:
                    stats["missing_relations"] += 1
                    stats["missing_relations_detail"].append(
                        {"doc_id": doc_id, "kind": "project", "slug": slug}
                    )
        if asset_slugs:
            qs = Asset.objects.filter(slug__in=asset_slugs)
            desired = set(qs.values_list("pk", flat=True))
            current = set(record.related_assets.values_list("pk", flat=True))
            if desired != current:
                record.related_assets.set(qs)
                changed = True
            found_slugs = set(qs.values_list("slug", flat=True))
            for slug in asset_slugs:
                if slug not in found_slugs:
                    stats["missing_relations"] += 1
                    stats["missing_relations_detail"].append(
                        {"doc_id": doc_id, "kind": "asset", "slug": slug}
                    )

        if created:
            stats["created"] += 1
        elif changed:
            stats["updated"] += 1
        else:
            stats["skipped"] += 1

        # Mirror public-article docs into ContentItem so they show up in
        # the publishing-pipeline KPIs and lists.
        if defaults["doc_type"] == DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT:
            _upsert_content_item(record, entry, defaults, stats)

    if report_orphans or prune_orphans:
        db_doc_ids = set(
            DocumentationRecord.objects.values_list("doc_id", flat=True)
        )
        orphan_ids = sorted(db_doc_ids - manifest_doc_ids)
        stats["orphans"] = orphan_ids

        if prune_orphans and orphan_ids:
            deleted_count, _by_model = (
                DocumentationRecord.objects.filter(doc_id__in=orphan_ids).delete()
            )
            stats["orphans_pruned"] = len(orphan_ids)
            stats["orphans_pruned_records"] = deleted_count

    return stats
