from django.core.checks import Error, Tags, register
from django.urls import NoReverseMatch, reverse

from application.integrations import IntegrationGraphError, integration_graph


def _route_error(owner: str, name: str, route: str, error_id: str):
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
def capability_contract_check(app_configs, **kwargs):  # noqa: ARG001
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
    except Exception as exc:
        return [
            Error(
                f"The composed integration graph is invalid: {exc}",
                id="hq_api.E001",
            )
        ]

    errors = []
    for spec in graph.resources.values():
        error = _route_error("Resource", spec.name, spec.web_route, "hq_api.E004")
        if error:
            errors.append(error)
    for spec in graph.connections.values():
        for route in (spec.web_route, spec.management_route, spec.setup_route):
            error = _route_error("Connection", spec.name, route, "hq_api.E006")
            if error:
                errors.append(error)
    return errors
