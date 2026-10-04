from __future__ import annotations

from application.integration_specs import ResourceSpec
from application.resources import BoundedQuery
from application.security import Capability

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
