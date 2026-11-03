"""Pull the example.com published-content index into ContentItems.

The live site owns "what is published": HQ reaches it over HTTP, authenticated
with a Cloudflare Access service token, and reflects it locally, as the GitHub
metadata refresh reflects a repo's `last_push_at`. Idempotent, keyed by slug.
"""

import json
from urllib.parse import urlsplit

import httpx2
from django.conf import settings
from django.utils.text import Truncator

from hq.domains.content.models import ContentItem
from hq.domains.projects.models import Project
from hq.platform.application.http_read import UNREACHABLE, fetch, redirect_target
from hq.platform.application.timestamps import moment


class ContentSyncError(RuntimeError):
    """Raised when the content index cannot be fetched or is malformed."""


def _parse_date(value):
    when = moment(value, naive="keep")
    return when.date() if when is not None else None


def _same_origin_redirects(response: httpx2.Response) -> None:
    """Follow a redirect only within the origin that was asked.

    The request carries an Access service token as headers of Cloudflare's own,
    which httpx2 keeps on a redirect wherever it points, since only
    ``Authorization`` is its to drop. A redirect to another origin, or down to
    http, is refused instead.
    """

    target = redirect_target(response)
    if target is not None and target.origin != response.url.origin:
        raise ContentSyncError("The content index redirected to another origin, which HQ does not follow.")


# An index of links is kilobytes. Read no further than this, so a site that
# answers with something else cannot make the fetch hold it all in memory.
MAX_INDEX_BYTES = 4 * 1024 * 1024


def fetch_content_index(url: str | None = None, timeout: int = 10) -> object:
    """GET the content index with the Cloudflare Access service-token headers: whatever JSON it holds."""
    url = url or settings.CONTENT_INDEX_URL
    if not url:
        raise ContentSyncError("CONTENT_INDEX_URL is not set.")
    headers = {
        "Accept": "application/json",
        # Cloudflare's browser-integrity check rejects an HTTP library's default
        # signature with Error 1010. Identify this machine client explicitly,
        # as every well-behaved HTTP adapter should.
        "User-Agent": "Severino-HQ/1.0 (+https://github.com/joeseverino/severino-hq)",
    }
    client_id = settings.CF_ACCESS_CLIENT_ID
    client_secret = settings.CF_ACCESS_CLIENT_SECRET
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    try:
        response, raw = fetch(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            limit=MAX_INDEX_BYTES,
            follow_redirects=True,
            on_response=(_same_origin_redirects,),
        )
    except UNREACHABLE as exc:
        raise ContentSyncError(f"Content index fetch failed: {exc}") from exc
    if not response.is_success:
        raise ContentSyncError(f"Content index HTTP {response.status_code}")
    if len(raw) > MAX_INDEX_BYTES:
        raise ContentSyncError("Content index is larger than an index can be.")
    try:
        return json.loads(raw.decode())
    except ValueError as exc:
        raise ContentSyncError(f"Content index fetch failed: {exc}") from exc


def index_project() -> Project | None:
    """The configured project, else the one whose public URL serves the index."""
    slug = settings.CONTENT_INDEX_PROJECT_SLUG
    if slug:
        return Project.objects.filter(slug=slug).first()
    host = urlsplit(settings.CONTENT_INDEX_URL).hostname
    if not host:
        return None
    matches = [
        project for project in Project.objects.exclude(public_url="") if urlsplit(project.public_url).hostname == host
    ]
    return matches[0] if len(matches) == 1 else None


def _live_fields(entry: dict, slug: str) -> dict:
    """What the published index says about one item, as ContentItem fields."""
    technologies = entry.get("technologies") or []
    return {
        "title": (entry.get("title") or slug)[:200],
        "status": ContentItem.Status.PUBLISHED,
        # A description longer than the field ends in an ellipsis.
        "topic": Truncator((entry.get("description") or "").strip()).chars(
            ContentItem._meta.get_field("topic").max_length
        ),
        "tags": ", ".join(str(t) for t in technologies if t)[:300],
        "published_url": entry.get("url") or "",
        "published_at": _parse_date(entry.get("published_at")),
    }


def _upsert(slug: str, live_fields: dict) -> tuple[ContentItem, str]:
    """Create or refresh one item: ``created``, ``updated`` or ``unchanged``.

    Content type is set on create only, so a manual classification survives.
    """
    item, was_created = ContentItem.objects.get_or_create(
        slug=slug,
        defaults={**live_fields, "content_type": ContentItem.Type.LAB_WRITEUP},
    )
    if was_created:
        return item, "created"
    changed = False
    for key, value in live_fields.items():
        if getattr(item, key) != value:
            setattr(item, key, value)
            changed = True
    if not changed:
        return item, "unchanged"
    item.save(update_fields=[*live_fields.keys(), "updated_at"])
    return item, "updated"


def sync_content_index(payload: object = None) -> dict:
    """Upsert ContentItems from the index, related to the site project.

    Pass ``payload`` to sync a pre-fetched index (used by tests); otherwise the
    index is fetched live. Returns a stats dict.
    """
    index = fetch_content_index() if payload is None else payload
    items = index.get("items") if isinstance(index, dict) else None
    if not isinstance(items, list):
        raise ContentSyncError("Content index payload has no 'items' list.")

    project = index_project()
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for entry in items:
        slug = (entry.get("slug") or "").strip() if isinstance(entry, dict) else ""
        if not slug:
            continue
        item, outcome = _upsert(slug, _live_fields(entry, slug))
        counts[outcome] += 1
        if project is not None:
            item.related_projects.add(project)

    return {
        "created": counts["created"],
        "updated": counts["updated"],
        "total": sum(counts.values()),
        "project": project.slug if project else None,
    }
