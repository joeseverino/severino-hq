"""The analytics resource, declared on its domain in application/domains.py."""

from typing import Annotated, TypedDict

from pydantic import Field, create_model

from hq.platform.application import analytics
from hq.platform.application.describe import Describe, listed
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import TopQuery, one_of
from hq.platform.application.security import Capability
from hq.platform.application.ui import ended

from .models import RumDaily

AnalyticsQuery = create_model(
    "AnalyticsQuery",
    # A breakdown is ranked biggest first, and its tail is narrowed by dimension or days rather than paged.
    __base__=TopQuery,
    # None means the default breakdown rather than "every breakdown at once":
    # the dimensions cannot be crossed, so a combined answer would be six
    # answers wearing one collection's shape.
    dimension=(
        one_of(RumDaily.Dimension.values) | None,
        Field(default=None, description="The one breakdown to answer; absent, by page."),
    ),
    days=(int, Field(default=28, ge=1, le=184, description="How many days back from today the numbers cover.")),
)


class Breakdown(TypedDict):
    """One value of a breakdown and what it was asked for."""

    dimension: Annotated[str, Describe(ended(f"Which breakdown it is in: {listed(RumDaily.Dimension.values)}"))]
    value: Annotated[
        str,
        Describe("The page, referrer, country, device, browser or operating system it counts, as the browser sent it."),
    ]
    visits: Annotated[int, Describe("How many visits it had over the days asked for, estimated from a sample.")]
    pageviews: Annotated[int, Describe("How many pages were viewed over the days asked for, estimated from a sample.")]


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
            row_type=Breakdown,
        ),
    )
