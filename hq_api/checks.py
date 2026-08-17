from django.core.checks import Error, Tags, register

from application.capabilities import capability_specs


@register(Tags.compatibility)
def capability_contract_check(app_configs, **kwargs):  # noqa: ARG001
    try:
        capability_specs()
    except Exception as exc:
        return [
            Error(
                f"The composed capability registry is invalid: {exc}",
                id="hq_api.E001",
            )
        ]
    return []
