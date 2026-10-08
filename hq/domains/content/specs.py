"""The content resource, declared on its domain in application/domains.py."""

from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import ContentItem


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "content",
            "Content",
            "Content records indexed by HQ.",
            Capability.READ,
            search=SearchDefinition(
                "content",
                ContentItem,
                "slug",
                ("title", "slug", "topic", "tags", "notes"),
                label="Content",
                title_field="title",
            ),
            web_route="content:list",
        ),
    )
