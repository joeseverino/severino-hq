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
from django.db.models import Count, F, Q

from assets.models import Asset
from content.models import ContentItem
from projects.models import Project

from . import frontmatter_schema
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


def _validate_choice(value: str, allowed, *, field: str) -> str:
    # `allowed` is a canonical set from frontmatter_schema — the same definition
    # the MCP validates writes against, so HQ never rejects a value the MCP just
    # wrote. Do not pass model `.choices` here; that would reintroduce drift.
    if value in allowed:
        return value
    raise ManifestImportError(
        f"Invalid {field}={value!r}. Allowed: {sorted(allowed)}"
    )


def _build_record_defaults(entry: dict) -> tuple[str, dict, bool]:
    """Validate an entry's enums and build the DocumentationRecord field defaults.

    Pure (no DB); raises ManifestImportError on an invalid choice. Shared by the
    importer's write path and the read-only validate path so the two can never
    disagree about what HQ accepts. A task carries its own status lifecycle
    (open/active/parked/done/wontfix), validated per-doc-type exactly as the MCP
    write path does; every other doc uses the standard status set.
    """
    doc_id = (entry.get("doc_id") or "").strip()
    doc_type = _validate_choice(
        entry.get("doc_type") or "runbook",
        frontmatter_schema.DOC_TYPES,
        field="doc_type",
    )
    is_task = doc_type == "task"
    status_allowed = (
        frontmatter_schema.TASK_STATUSES if is_task else frontmatter_schema.STATUSES
    )
    status_default = "open" if is_task else "draft"
    defaults = {
        "title": entry.get("title") or doc_id,
        "doc_type": doc_type,
        "system_service": entry.get("system") or entry.get("system_service") or "",
        "environment": _validate_choice(
            entry.get("environment") or "other",
            frontmatter_schema.ENVIRONMENTS,
            field="environment",
        ),
        "status": _validate_choice(
            entry.get("status") or status_default,
            status_allowed,
            field="status",
        ),
        "sensitivity": _validate_choice(
            entry.get("sensitivity") or "internal",
            frontmatter_schema.SENSITIVITIES,
            field="sensitivity",
        ),
        "obsidian_path": entry.get("path") or entry.get("obsidian_path") or "",
        "github_path": entry.get("github_path") or "",
        "external_url": entry.get("external_url") or "",
        "last_reviewed": _coerce_date(entry.get("last_reviewed")),
        "published_at": _coerce_date(entry.get("published_at")),
        "notes": entry.get("notes") or "",
    }
    return doc_id, defaults, is_task


def validate_manifest_data(items: Iterable[dict]) -> list[dict]:
    """Read-only preflight: validate every entry's enums against the canonical
    schema WITHOUT touching the database, so contract drift (an invalid status /
    doc_type / environment / sensitivity — the class that wedged `hq sync`) is
    caught locally before the deployed importer ever runs. Returns a list of
    ``{doc_id, errors:[...]}`` for entries that fail; empty means the manifest is
    importable. Validates the same enums the write path does, via the shared
    ``_build_record_defaults``.
    """
    if not isinstance(items, list):
        raise ManifestImportError("Manifest must be a JSON array of records.")
    problems: list[dict] = []
    for entry in items:
        doc_id = (entry.get("doc_id") or "").strip()
        if not doc_id:
            problems.append({"doc_id": "", "errors": ["missing doc_id"]})
            continue
        try:
            _build_record_defaults(entry)
        except ManifestImportError as exc:
            problems.append({"doc_id": doc_id, "errors": [str(exc)]})
    return problems


def _sync_relation(record, manager, slugs, *, kind: str, doc_id: str, stats: dict):
    """Set a record's M2M relation to the registry rows matching ``slugs`` and
    record any slug with no matching row as a missing relation. One implementation
    for both projects and assets (was copy-pasted). Returns the resolved queryset
    (so the caller can, e.g., backfill project tech) and whether it changed.
    """
    qs = manager.filter(slug__in=slugs)
    desired = set(qs.values_list("pk", flat=True))
    relation = getattr(record, f"related_{kind}s")
    changed = desired != set(relation.values_list("pk", flat=True))
    if changed:
        relation.set(qs)
    found = set(qs.values_list("slug", flat=True))
    for slug in slugs:
        if slug not in found:
            stats["missing_relations"] += 1
            stats["missing_relations_detail"].append(
                {"doc_id": doc_id, "kind": kind, "slug": slug}
            )
    return qs, changed


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


def _legacy_content_slug_from_doc_id(doc_id: str) -> str:
    """Return the pre-manifest-slug ContentItem slug for a doc_id."""
    slug = doc_id
    for prefix in ("writeup-", "page-", "article-"):
        if slug.startswith(prefix):
            return slug.removeprefix(prefix)
    return slug


def _prune_legacy_content_item_for_record(
    record: "DocumentationRecord",
    stats: dict,
) -> None:
    """Remove a stale mirrored ContentItem for records no longer in content."""
    content_items_to_prune = (
        ContentItem.objects.filter(
            slug=_legacy_content_slug_from_doc_id(record.doc_id),
            related_documentation=record,
        )
        .annotate(documentation_count=Count("related_documentation", distinct=True))
        .filter(documentation_count=1)
    )
    content_items_pruned = content_items_to_prune.count()
    if content_items_pruned:
        content_items_to_prune.delete()
        stats.setdefault("content_items_pruned", 0)
        stats["content_items_pruned"] += content_items_pruned


def _upsert_content_item(
    record: "DocumentationRecord",
    entry: dict,
    defaults: dict,
    stats: dict,
) -> None:
    """Mirror a public-article DocumentationRecord into ContentItem.

    Keyed by the manifest slug when provided, falling back to a slug derived
    from `doc_id` for older manifests. Idempotent: if a ContentItem with that
    slug already exists, its fields are refreshed.
    """
    doc_id = record.doc_id
    article_slug = (entry.get("slug") or "").strip()
    if not article_slug:
        article_slug = _legacy_content_slug_from_doc_id(doc_id)

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


def _tag_list(entry: dict) -> list[str]:
    tags = entry.get("tags") or entry.get("technologies") or []
    if isinstance(tags, list):
        return [str(tag).strip() for tag in tags if str(tag).strip()]
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(",") if tag.strip()]
    return []


def _backfill_project_technologies(projects, entry: dict, stats: dict) -> None:
    """Hydrate empty Project.tech from linked manifest tags.

    The vault already knows project technology tags on architecture/writeup
    docs. Keep manually curated Project.tech values authoritative, but avoid
    blank HQ project rows when the manifest has enough metadata to help.
    """
    tags = _tag_list(entry)
    if not tags:
        return

    techs = ", ".join(tags)
    changed = 0
    for project in projects:
        if project.technologies_used:
            continue
        project.technologies_used = techs
        project.save(update_fields=["technologies_used", "updated_at"])
        changed += 1

    if changed:
        stats.setdefault("projects_tech_backfilled", 0)
        stats["projects_tech_backfilled"] += changed


@transaction.atomic
def import_manifest_data(
    items: Iterable[dict],
    *,
    update_existing: bool = True,
    report_orphans: bool = False,
    prune_orphans: bool = False,
) -> dict[str, Any]:
    """Upsert the manifest and return a stats dict.

    The returned ``stats`` is the contract `import_docs_manifest --json` emits
    and the `hq sync` wrapper parses. Keys (treat as additive — do not rename):

    - ``created`` / ``updated`` / ``skipped`` — DocumentationRecord counts.
    - ``missing_relations`` (int) + ``missing_relations_detail`` (list of
      ``{doc_id, kind, slug}``) — doc relations pointing at an absent registry slug.
    - ``content_items_synced`` / ``content_items_pruned`` — mirrored ContentItem counts.
    - ``orphans`` (list of doc_id) + ``orphans_pruned`` / ``orphans_pruned_records``
      — present only when ``report_orphans``/``prune_orphans``.
    """
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

        _, defaults, _ = _build_record_defaults(entry)

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
            qs, rel_changed = _sync_relation(
                record, Project.objects, project_slugs, kind="project", doc_id=doc_id, stats=stats
            )
            changed = changed or rel_changed
            _backfill_project_technologies(qs, entry, stats)
        if asset_slugs:
            _, rel_changed = _sync_relation(
                record, Asset.objects, asset_slugs, kind="asset", doc_id=doc_id, stats=stats
            )
            changed = changed or rel_changed

        if created:
            stats["created"] += 1
        elif changed:
            stats["updated"] += 1
        else:
            stats["skipped"] += 1

        # Only explicit content entries mirror into ContentItem. Some reporting
        # docs use public_article_draft as a writing state, but they are not
        # part of the site CMS unless the manifest carries content_type.
        if (
            defaults["doc_type"] == DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT
            and entry.get("content_type")
        ):
            _upsert_content_item(record, entry, defaults, stats)
        elif defaults["doc_type"] == DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT:
            _prune_legacy_content_item_for_record(record, stats)

    if report_orphans or prune_orphans:
        db_doc_ids = set(
            DocumentationRecord.objects.values_list("doc_id", flat=True)
        )
        orphan_ids = sorted(db_doc_ids - manifest_doc_ids)
        stats["orphans"] = orphan_ids

        if prune_orphans and orphan_ids:
            orphan_docs = DocumentationRecord.objects.filter(doc_id__in=orphan_ids)
            content_items_to_prune = (
                ContentItem.objects.filter(related_documentation__in=orphan_docs)
                .annotate(
                    documentation_count=Count("related_documentation", distinct=True),
                    orphan_documentation_count=Count(
                        "related_documentation",
                        filter=Q(related_documentation__in=orphan_docs),
                        distinct=True,
                    ),
                )
                .filter(documentation_count=F("orphan_documentation_count"))
            )
            content_items_pruned = content_items_to_prune.count()
            content_items_to_prune.delete()

            deleted_count, _by_model = (
                orphan_docs.delete()
            )
            stats["orphans_pruned"] = len(orphan_ids)
            stats["orphans_pruned_records"] = deleted_count
            stats["content_items_pruned"] = content_items_pruned

    return stats
