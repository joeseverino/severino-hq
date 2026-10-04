"""The receipts resource, declared on its domain in application/domains.py."""

from __future__ import annotations

from application import read_models
from application.integration_specs import ResourceSpec
from application.resources import BoundedQuery
from application.search_contracts import SearchDefinition
from application.security import Capability

from .models import Receipt


class ReceiptQuery(BoundedQuery):
    unmatched_only: bool = False


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
