"""Content commands shared by the web, MCP, and CLI adapters."""

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from django.db import transaction

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.core.audit import operation_context

from .domains import records_of
from .entity_links import EntityLink, entity_link
from .labels import plural
from .projection import iso
from .security import Principal
from .sensitivity import safe_doc_ids
from .ui import counted


def published_on(urls) -> EntityLink | None:
    """The one site every one of ``urls`` is on, as a link to the project that publishes there.

    None when nothing is published or the addresses are on more than one
    site. A site no project publishes is named without a page.
    """

    from urllib.parse import urlsplit

    from .entity_links import web_url
    from .published_sites import projects_by_hostname

    hosts = {urlsplit(web_url(url)).hostname for url in urls if url}
    hosts.discard(None)
    if len(hosts) != 1:
        return None
    (host,) = hosts
    project = projects_by_hostname().get(host)
    if project is None:
        return EntityLink(label=host, kind="service", kind_label="Site")
    return entity_link("project", project.slug, label=host)


class NotFoundError(ValueError):
    """A content item or requested relationship does not exist."""


class ConflictError(ValueError):
    """A content item changed after the caller read it."""


@dataclass(frozen=True, slots=True)
class ContentCommand:
    title: str
    slug: str = ""
    content_type: str = ContentItem.Type.ARTICLE
    status: str = ContentItem.Status.DRAFT
    topic: str = ""
    tags: str = ""
    published_url: str = ""
    wordpress_post_id: int | None = None
    wordpress_slug: str = ""
    published_at: date | None = None
    notes: str = ""
    related_projects: tuple[str, ...] = ()
    related_assets: tuple[str, ...] = ()
    related_expenses: tuple[int, ...] = ()
    related_documentation: tuple[str, ...] = ()


def serialize_content(item: ContentItem) -> dict[str, Any]:
    return {
        "slug": item.slug,
        "title": item.title,
        "content_type": item.content_type,
        "status": item.status,
        "topic": item.topic,
        "tags": item.tag_list,
        "published_url": item.published_url,
        "wordpress_post_id": item.wordpress_post_id,
        "wordpress_slug": item.wordpress_slug,
        "published_at": iso(item.published_at),
        "notes": item.notes,
        "updated_at": iso(item.updated_at),
        "relationships": {
            "projects": list(item.related_projects.order_by("slug").values_list("slug", flat=True)),
            "assets": list(item.related_assets.order_by("slug").values_list("slug", flat=True)),
            "expense_ids": list(item.related_expenses.order_by("id").values_list("id", flat=True)),
            "documentation": safe_doc_ids(item.related_documentation),
        },
    }


def _resolve(model, field: str, values: tuple, label: str):
    records = list(model.objects.filter(**{f"{field}__in": values}))
    found = {getattr(record, field) for record in records}
    missing = sorted(set(values) - found)
    if missing:
        found_none = counted(len(missing), f"related {label} not found", f"related {plural(label)} not found")
        raise NotFoundError(f"{found_none}: {', '.join(map(str, missing))}")
    return records


@transaction.atomic
def save_content(
    command: ContentCommand,
    *,
    principal: Principal,
    current_slug: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(records_of("content").write)
    operation = "content.create" if current_slug is None else "content.update"
    with operation_context(interface=principal.interface, actor=principal.actor, operation=operation):
        if current_slug is None:
            item = ContentItem()
            created = True
        else:
            try:
                item = ContentItem.objects.select_for_update().get(slug=current_slug)
            except ContentItem.DoesNotExist as exc:
                raise NotFoundError(f"The writeup or page {current_slug!r} was not found.") from exc
            created = False
            if expected_updated_at and item.updated_at.isoformat() != expected_updated_at:
                raise ConflictError(f"The writeup or page {current_slug!r} changed after it was read.")

        values = asdict(command)
        projects = _resolve(Project, "slug", values.pop("related_projects"), "project")
        assets = _resolve(Asset, "slug", values.pop("related_assets"), "asset")
        expenses = _resolve(Expense, "id", values.pop("related_expenses"), "expense")
        docs = _resolve(
            DocumentationRecord,
            "doc_id",
            values.pop("related_documentation"),
            "documentation record",
        )
        for field, value in values.items():
            setattr(item, field, value)
        item.full_clean()
        item.save()
        item.related_projects.set(projects)
        item.related_assets.set(assets)
        item.related_expenses.set(expenses)
        item.related_documentation.set(docs)

    return {"ok": True, "created": created, "content": serialize_content(item)}
