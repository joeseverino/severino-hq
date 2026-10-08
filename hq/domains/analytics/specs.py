"""The analytics resource, declared on its domain in application/domains.py."""

from pydantic import Field

from hq.platform.application import analytics
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.security import Capability


class AnalyticsQuery(BoundedQuery):
    # Empty means the default breakdown rather than "every breakdown at once":
    # the dimensions cannot be crossed, so a combined answer would be six
    # answers wearing one collection's shape.
    dimension: str = ""
    days: int = Field(default=28, ge=1, le=184)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "analytics",
            "Analytics",
            "What the published site was asked for, by any breakdown HQ records.",
            Capability.READ,
            analytics.list_analytics,
            AnalyticsQuery,
            web_route="analytics:overview",
        ),
    )
