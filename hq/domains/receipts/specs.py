"""The receipts resource, declared on its domain in application/domains.py."""

from pydantic import Field

from hq.platform.application import read_models
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import Receipt


class ReceiptQuery(BoundedQuery):
    unmatched_only: bool = Field(
        default=False, description="True for only the receipts matched to no expense and no asset."
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "receipts",
            "Receipts",
            "Receipt metadata without file contents, storage paths, or URLs.",
            Capability.READ,
            read_models.list_receipts,
            ReceiptQuery,
            search=SearchDefinition(
                "receipts",
                Receipt,
                "pk",
                ("original_filename", "vendor", "notes"),
                label="Receipts",
            ),
            web_route="receipts:list",
        ),
    )
