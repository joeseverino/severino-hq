from django.core.checks import Error, Tags, register
from django.urls import NoReverseMatch, reverse

from application.capabilities import capability_specs
from application.connections import connection_specs
from application.resources import resource_specs


def _registry(factory, label: str, error_id: str):
    try:
        return factory(), []
    except Exception as exc:
        return (), [
            Error(
                f"The composed {label} registry is invalid: {exc}",
                id=error_id,
            )
        ]


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
    capabilities, errors = _registry(
        capability_specs, "capability", "hq_api.E001"
    )
    resources, resource_errors = _registry(resource_specs, "resource", "hq_api.E002")
    connections, connection_errors = _registry(
        connection_specs, "connection", "hq_api.E005"
    )
    errors.extend(resource_errors)
    errors.extend(connection_errors)
    if errors:
        return errors

    known_resources = {spec.name for spec in resources}
    missing_resources = sorted(
        {
            spec.subject_resource
            for spec in capabilities
            if spec.subject_resource and spec.subject_resource not in known_resources
        }
    )
    if missing_resources:
        errors.append(
            Error(
                "Capabilities reference unknown resources: "
                f"{', '.join(missing_resources)}.",
                id="hq_api.E003",
            )
        )
    for spec in resources:
        error = _route_error("Resource", spec.name, spec.web_route, "hq_api.E004")
        if error:
            errors.append(error)
    for spec in connections:
        for route in (spec.web_route, spec.management_route, spec.setup_route):
            error = _route_error("Connection", spec.name, route, "hq_api.E006")
            if error:
                errors.append(error)
    known_capabilities = {spec.name for spec in capabilities}
    unknown_abilities = sorted(
        {
            ability.capability
            for spec in connections
            for ability in spec.abilities
            if ability.capability and ability.capability not in known_capabilities
        }
    )
    if unknown_abilities:
        errors.append(
            Error(
                "Connection abilities reference unknown capabilities: "
                f"{', '.join(unknown_abilities)}.",
                id="hq_api.E007",
            )
        )
    return errors
