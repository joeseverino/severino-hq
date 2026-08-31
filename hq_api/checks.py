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


def _target_query_errors(capabilities, resource_by_name):
    errors = []
    for capability in capabilities:
        if not capability.target_query or not capability.subject_resource:
            continue
        resource = resource_by_name.get(capability.subject_resource)
        if resource is None:
            continue
        if not resource.list_handler or not resource.list_query_type:
            errors.append(
                Error(
                    f"Capability {capability.name!r} cannot derive targets from "
                    f"unlistable resource {resource.name!r}.",
                    id="hq_api.E008",
                )
            )
            continue
        try:
            resource.list_query_type.model_validate(
                dict(capability.target_query), strict=True
            )
        except Exception as exc:
            errors.append(
                Error(
                    f"Capability {capability.name!r} has an invalid target query "
                    f"for {resource.name!r}: {exc}",
                    id="hq_api.E008",
                )
            )
    return errors


@register(Tags.compatibility)
def capability_contract_check(app_configs, **kwargs):  # noqa: ARG001
    capabilities, errors = _registry(capability_specs, "capability", "hq_api.E001")
    resources, resource_errors = _registry(resource_specs, "resource", "hq_api.E002")
    connections, connection_errors = _registry(
        connection_specs, "connection", "hq_api.E005"
    )
    errors.extend(resource_errors)
    errors.extend(connection_errors)
    if errors:
        return errors

    resource_by_name = {spec.name: spec for spec in resources}
    known_resources = set(resource_by_name)
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
    errors.extend(_target_query_errors(capabilities, resource_by_name))
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
    # E007 proves the capability exists; this proves it serves the family that
    # named it. Undeclared `exercises` stays legal, so only a stated
    # disagreement is an error.
    capability_by_name = {spec.name: spec for spec in capabilities}
    disagreements = sorted(
        {
            f"{spec.name} -> {ability.capability}"
            for spec in connections
            for ability in spec.abilities
            if ability.capability
            and (served := capability_by_name.get(ability.capability)) is not None
            and served.exercises
            and spec.name not in served.exercises
        }
    )
    if disagreements:
        errors.append(
            Error(
                "Connection abilities name capabilities that do not serve them: "
                f"{', '.join(disagreements)}.",
                hint=(
                    "Either the ability names the wrong command, or the "
                    "capability's `exercises` should include this family."
                ),
                id="hq_api.E010",
            )
        )
    unknown_ability_resources = sorted(
        {
            ability.subject_resource
            for spec in connections
            for ability in spec.abilities
            if ability.subject_resource
            and ability.subject_resource not in known_resources
        }
    )
    if unknown_ability_resources:
        errors.append(
            Error(
                "Connection abilities reference unknown resources: "
                f"{', '.join(unknown_ability_resources)}.",
                id="hq_api.E009",
            )
        )
    return errors
