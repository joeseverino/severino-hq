from __future__ import annotations

from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.security import Capability

from .service import list_widgets


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "widgets",
            "Widgets",
            "The fixture domain's records.",
            Capability.READ,
            list_widgets,
            BoundedQuery,
            web_route="widgets:list",
        ),
    )
