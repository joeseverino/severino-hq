"""The documentation resource, declared on its domain in application/domains.py."""

from pydantic import Field, create_model

from hq.platform.application import documentation
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resource_records import DocumentationRow
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import DocumentationRecord

DocumentationQuery = create_model(
    "DocumentationQuery",
    __base__=BoundedQuery,
    status=(one_of(DocumentationRecord.Status.values) | None, Field(default=None, description=STATUS_FILTER)),
    query=(TextFilter, None),
)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "documentation",
            "Docs",
            "Sensitivity-aware documentation pointers indexed by HQ.",
            Capability.READ,
            documentation.list_documentation,
            DocumentationQuery,
            search=SearchDefinition(
                "documentation",
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
                label="Docs",
                title_field="title",
                badge_field="doc_id",
            ),
            web_route="docs_index:list",
            row_type=DocumentationRow,
        ),
    )
