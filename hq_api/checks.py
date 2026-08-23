from django.core.checks import Error, Tags, register
from django.urls import NoReverseMatch, reverse

from application.capabilities import capability_specs
from application.resources import resource_specs


@register(Tags.compatibility)
def capability_contract_check(app_configs, **kwargs):  # noqa: ARG001
    try:
        capabilities = capability_specs()
    except Exception as exc:
        return [
            Error(
                f"The composed capability registry is invalid: {exc}",
                id="hq_api.E001",
            )
        ]
    try:
        resources = resource_specs()
    except Exception as exc:
        return [
            Error(
                f"The composed resource registry is invalid: {exc}",
                id="hq_api.E002",
            )
        ]

    known_resources = {spec.name for spec in resources}
    missing_resources = sorted(
        {
            spec.subject_resource
            for spec in capabilities
            if spec.subject_resource and spec.subject_resource not in known_resources
        }
    )
    errors = []
    if missing_resources:
        errors.append(
            Error(
                "Capabilities reference unknown resources: "
                f"{', '.join(missing_resources)}.",
                id="hq_api.E003",
            )
        )
    for spec in resources:
        if not spec.web_route:
            continue
        try:
            reverse(spec.web_route)
        except NoReverseMatch as exc:
            errors.append(
                Error(
                    f"Resource {spec.name!r} has an unusable web route "
                    f"{spec.web_route!r}: {exc}",
                    id="hq_api.E004",
                )
            )
    return errors
