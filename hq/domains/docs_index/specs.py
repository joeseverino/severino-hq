"""The documentation resource, declared on its domain in application/domains.py."""

from __future__ import annotations

from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import DocumentationRecord


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "documentation",
            "Docs",
            "Sensitivity-aware documentation pointers indexed by HQ.",
            Capability.READ,
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
        ),
    )
