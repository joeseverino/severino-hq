"""The documentation's commands and resource, declared on its domain in application/domains.py."""

from django.db.models import Q

from hq.platform.application import documentation
from hq.platform.application.contracts import Effect
from hq.platform.application.documentation import DocumentationItem, DocumentationRow
from hq.platform.application.integration_specs import (
    CapabilitySpec,
    DetailRead,
    ListFilter,
    ListRead,
    ListSort,
    ResourceSpec,
)
from hq.platform.application.resources import BoundedQuery, SearchText
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import DocumentationRecord


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            documentation.SYNC,
            "Bring HQ's list of docs up to date from a vault export.",
            Effect.REMOTE_WRITE,
            # Documentation authority alone, so an account allowed to sync docs
            # needs no control-plane authority.
            Capability.SYNC_DOCUMENTATION,
            documentation.DocumentationSyncCommand,
            documentation.execute_documentation_sync,
            subject_resource="documentation",
            label="Update docs from an export",
            verb="Update docs",
        ),
    )


class DocumentationQuery(BoundedQuery):
    query: SearchText = None


# Each order the list is read in, the first where none is asked.
SORTS = (
    *ListSort.both("-updated_at", "Recently updated", "Least recently updated"),
    *ListSort.both("doc_id", "ID"),
    *ListSort.both("title", "Title"),
    *ListSort.both("doc_type", "Type"),
    *ListSort.both("environment", "Environment"),
    *ListSort.both("last_reviewed", "Oldest change", "Newest change"),
    *ListSort.both("status", "Status"),
    *ListSort.both("sensitivity", "Sensitivity"),
)
FILTERS = (
    ListFilter("status", "Status", DocumentationRecord.Status.choices),
    ListFilter("environment", "Environment", DocumentationRecord.Environment.choices),
    # Writeups and pages are the Content section's, so the list leaves them
    # out until a type or a search asks for them.
    ListFilter(
        "doc_type",
        "Type",
        DocumentationRecord.DocType.choices,
        unasked=~Q(doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT),
    ),
    ListFilter("sensitivity", "Sensitivity", DocumentationRecord.Sensitivity.choices),
)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "documentation",
            "Docs",
            "Sensitivity-aware documentation pointers indexed by HQ.",
            Capability.READ,
            listing=ListRead(
                documentation.list_documentation,
                DocumentationQuery,
                DocumentationRow,
                sorts=SORTS,
                filters=FILTERS,
                totals=documentation.list_totals,
            ),
            detail=DetailRead(documentation.get_documentation, DocumentationItem),
            identifier="doc_id",
            pass_principal=True,
            search=SearchDefinition.of(
                DocumentationRecord,
                "doc_id",
                (
                    "doc_id",
                    "title",
                    "system_service",
                    "obsidian_path",
                    "github_path",
                    "notes",
                ),
                title_field="title",
                badge_field="doc_id",
            ),
            web_route="docs_index:list",
        ),
    )
