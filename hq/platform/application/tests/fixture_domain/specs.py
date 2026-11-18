from typing import TypedDict

from hq.platform.application.integration_specs import ListRead, ResourceSpec
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.security import Capability

from .service import list_widgets


class WidgetRow(TypedDict):
    slug: str
    name: str


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "widgets",
            "Widgets",
            "The fixture domain's records.",
            Capability.READ,
            listing=ListRead(list_widgets, BoundedQuery, WidgetRow),
            web_route="widgets:list",
        ),
    )
