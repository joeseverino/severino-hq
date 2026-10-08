from collections.abc import Sequence
from typing import Any

from django.apps import AppConfig
from django.core.checks import CheckMessage, Error, Tags, register
from django.urls import NoReverseMatch, reverse

from hq.platform.application.integrations import IntegrationGraphError, integration_graph


def _route_error(owner: str, name: str, route: str, error_id: str) -> Error | None:
    if not route:
        return None
    try:
        reverse(route)
    except NoReverseMatch as exc:
        return Error(
            f"{owner} {name!r} has an unusable web route {route!r}: {exc}",
            id=error_id,
        )
    return None


@register(Tags.compatibility)
def capability_contract_check(
    app_configs: Sequence[AppConfig] | None, **kwargs: Any
) -> list[CheckMessage]:
    try:
        graph = integration_graph()
    except IntegrationGraphError as exc:
        return [
            Error(
                violation.message,
                hint=f"Integration violation: {violation.code}",
                id="hq_api.E001",
            )
            for violation in exc.violations
        ]
    except Exception as exc:  # noqa: BLE001 - any failure to build the graph is reported as a check error
        return [
            Error(
                f"The composed integration graph is invalid: {exc}",
                id="hq_api.E001",
            )
        ]

    errors: list[CheckMessage] = []
    for spec in graph.resources.values():
        error = _route_error("Resource", spec.name, spec.web_route, "hq_api.E004")
        if error:
            errors.append(error)
    for connection in graph.connections.values():
        routes = (connection.web_route, connection.management_route, connection.setup_route)
        for route in routes:
            error = _route_error("Connection", connection.name, route, "hq_api.E006")
            if error:
                errors.append(error)
    return errors
