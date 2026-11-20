"""The documentation's commands and resource, declared on its domain in application/domains.py."""

from pydantic import Field, create_model

from hq.platform.application import documentation
from hq.platform.application.contracts import Effect
from hq.platform.application.documentation import DocumentationItem, DocumentationRow
from hq.platform.application.integration_specs import CapabilitySpec, ListRead, ResourceSpec
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, SearchText, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability
from hq.platform.application.sync import HQSyncCommand, execute_hq_sync

from .models import DocumentationRecord


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            "hq.sync",
            "Bring HQ's list of docs up to date from the vault.",
            Effect.REMOTE_WRITE,
            # Documentation authority alone, so an account allowed to sync docs
            # needs no control-plane authority.
            Capability.SYNC_DOCUMENTATION,
            HQSyncCommand,
            execute_hq_sync,
            subject_resource="documentation",
            label="Update docs from the vault",
        ),
        CapabilitySpec(
            documentation.SYNC,
            "Bring HQ's list of docs up to date from a vault export.",
            Effect.REMOTE_WRITE,
            Capability.SYNC_DOCUMENTATION,
            documentation.DocumentationSyncCommand,
            documentation.execute_documentation_sync,
            subject_resource="documentation",
            label="Update docs from an export",
            verb="Update docs",
        ),
    )


DocumentationQuery = create_model(
    "DocumentationQuery",
    __base__=BoundedQuery,
    status=(one_of(DocumentationRecord.Status.values) | None, Field(default=None, description=STATUS_FILTER)),
    query=(SearchText, None),
)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "documentation",
            "Docs",
            "Sensitivity-aware documentation pointers indexed by HQ.",
            Capability.READ,
            listing=ListRead(documentation.list_documentation, DocumentationQuery, DocumentationRow),
            detail_handler=documentation.get_documentation,
            identifier="doc_id",
            pass_principal=True,
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
            record_type=DocumentationItem,
        ),
    )
