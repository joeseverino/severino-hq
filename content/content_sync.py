"""Pull the jseverino.com published-content index into ContentItems.

Mirrors the GitHub metadata refresh: HQ reaches an already-public external
source over HTTP, authenticated with a Cloudflare Access service token, and
reflects it locally. The live site is the owner of "what is published"; HQ
reflects it, exactly as it reflects the GitHub repo for `last_push_at`.

Idempotent, keyed by slug. Content type is set on create only, so a manual
classification in HQ is never clobbered by a sync.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

from django.conf import settings

from content.models import ContentItem
from projects.models import Project


class ContentSyncError(RuntimeError):
    """Raised when the content index cannot be fetched or is malformed."""


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def fetch_content_index(url: str | None = None, timeout: int = 10) -> dict:
    """GET the content index with the Cloudflare Access service-token headers."""
    url = url or settings.CONTENT_INDEX_URL
    headers = {"Accept": "application/json"}
    client_id = getattr(settings, "CF_ACCESS_CLIENT_ID", "")
    client_secret = getattr(settings, "CF_ACCESS_CLIENT_SECRET", "")
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ContentSyncError(f"Content index HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise ContentSyncError(f"Content index fetch failed: {exc}") from exc


def sync_content_index(payload: dict | None = None) -> dict:
    """Upsert ContentItems from the index, related to the site project.

    Pass ``payload`` to sync a pre-fetched index (used by tests); otherwise the
    index is fetched live. Returns a stats dict.
    """
    if payload is None:
        payload = fetch_content_index()
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ContentSyncError("Content index payload has no 'items' list.")

    project = Project.objects.filter(
        slug=getattr(settings, "CONTENT_INDEX_PROJECT_SLUG", "")
    ).first()

    created = updated = total = 0
    for entry in items:
        if not isinstance(entry, dict):
            continue
        slug = (entry.get("slug") or "").strip()
        if not slug:
            continue
        total += 1

        technologies = entry.get("technologies") or []
        tags_str = ", ".join(str(t) for t in technologies if t)[:300]
        live_fields = {
            "title": (entry.get("title") or slug)[:200],
            "status": ContentItem.Status.PUBLISHED,
            "topic": (entry.get("description") or "").strip()[:160],
            "tags": tags_str,
            "published_url": entry.get("url") or "",
            "published_at": _parse_date(entry.get("published_at")),
        }
        create_defaults = {
            **live_fields,
            "content_type": ContentItem.Type.LAB_WRITEUP,
        }

        item, was_created = ContentItem.objects.get_or_create(
            slug=slug, defaults=create_defaults
        )
        if was_created:
            created += 1
        else:
            changed = False
            for key, value in live_fields.items():
                if getattr(item, key) != value:
                    setattr(item, key, value)
                    changed = True
            if changed:
                item.save(update_fields=[*live_fields.keys(), "updated_at"])
                updated += 1

        if project is not None:
            item.related_projects.add(project)

    return {
        "created": created,
        "updated": updated,
        "total": total,
        "project": project.slug if project else None,
    }
