"""Content commands shared by the web, MCP, and CLI adapters."""

from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Annotated, Any
lazy from urllib.parse import urlsplit

from hq.domains.content.models import ContentItem

from .command_fields import Describe, Names
from .entity_links import EntityLink, entity_link, web_url
from .projection import iso, listing
from .record_saves import Held, Link, linked_saver
from .sensitivity import safe_doc_ids
lazy from .published_sites import projects_by_hostname


def published_on(urls) -> EntityLink | None:
    """The one site every one of ``urls`` is on, as a link to the project that publishes there.

    None when nothing is published or the addresses are on more than one
    site. A site no project publishes is named without a page.
    """

    hosts = {urlsplit(web_url(url)).hostname for url in urls if url}
    hosts.discard(None)
    if len(hosts) != 1:
        return None
    (host,) = hosts
    project = projects_by_hostname().get(host)
    if project is None:
        return EntityLink(label=host, kind="service", kind_label="Site")
    return entity_link("project", project.slug, label=host)


@dataclass(frozen=True, slots=True)
class ContentCommand:
    title: Annotated[str, Describe("What it is called.")]
    slug: Annotated[str, Describe("Its address in HQ; blank derives one from the title.")] = ""
    content_type: Annotated[str, Describe("What type of writing it is.")] = ContentItem.Type.ARTICLE
    status: Annotated[str, Describe("Where it stands, from draft to published.")] = ContentItem.Status.DRAFT
    topic: Annotated[str, Describe("What it is about, in a few words.")] = ""
    tags: Annotated[str, Describe("Tags, separated by commas.")] = ""
    published_url: Annotated[str, Describe("Where it is published.")] = ""
    published_at: Annotated[date | None, Describe("When it was published.")] = None
    notes: Annotated[str, Describe("Anything else worth keeping.")] = ""
    related_projects: Annotated[tuple[str, ...], Names("projects"), Describe("The projects it is about.")] = ()
    related_assets: Annotated[tuple[str, ...], Names("assets"), Describe("The assets it is about.")] = ()
    related_expenses: Annotated[
        tuple[int, ...], Names("expenses", key="id"), Describe("The expenses it accounts for.")
    ] = ()
    related_documentation: Annotated[
        tuple[str, ...], Names("documentation", key="doc_id"), Describe("The docs it draws on.")
    ] = ()


def serialize_content(item: ContentItem, *, relationships: bool = True) -> dict[str, Any]:
    fields = {
        "slug": item.slug,
        "title": item.title,
        "content_type": item.content_type,
        "status": item.status,
        "topic": item.topic,
        "tags": item.tag_list,
        "published_url": item.published_url,
        "published_at": iso(item.published_at),
        "notes": item.notes,
        "updated_at": iso(item.updated_at),
    }
    if relationships:
        fields["relationships"] = {
            "projects": list(item.related_projects.order_by("slug").values_list("slug", flat=True)),
            "assets": list(item.related_assets.order_by("slug").values_list("slug", flat=True)),
            "expense_ids": list(item.related_expenses.order_by("id").values_list("id", flat=True)),
            "documentation": safe_doc_ids(item.related_documentation),
        }
    return fields


def list_content(*, status: str | None = None, query: str | None = None, limit: int = 50) -> dict[str, Any]:
    return listing(
        ContentItem,
        partial(serialize_content, relationships=False),
        search=("title", "slug", "topic"),
        status=status,
        query=query,
        limit=limit,
    )


save_content = linked_saver(
    "content",
    serialize_content,
    refusals=Held("That writeup or page was not found.", "That writeup or page changed after it was read."),
    links={
        "related_projects": Link("slug", "A related project was not found."),
        "related_assets": Link("slug", "A related asset was not found."),
        "related_expenses": Link("id", "A related expense was not found."),
        "related_documentation": Link("doc_id", "A related documentation record was not found."),
    },
)
