"""A proxy in front of HQ that refuses a request HQ takes.

A proxy refuses a body over its limit itself, so the request never reaches HQ
and the browser shows its own error page. The limit HQ needs is derived from
every file field composed (``uploads``).
"""

from hq.domains.control_plane.providers import PROVIDERS

from .finding_model import Finding, FindingEstate, FindingRemedy, FindingRule
from .references import Kind
from .tone import Tone
lazy from . import uploads
lazy from .hq_self import hq_hostnames
lazy from .infrastructure import enabled_resources
lazy from .resource_operations import ALLOW_UPLOADS
lazy from .service_declarations import declarations


def _refusing(_estate: FindingEstate) -> tuple[Finding, ...]:
    """Each proxy declared for one of HQ's names whose body limit is below the largest request HQ takes."""

    declared = declarations().declared
    serving = {claim.resource_key for name in hq_hostnames() for claim in declared.get(name, {}).get("proxy", ())}
    if not serving:
        return ()
    needed = uploads.proxy_body_mib()
    found = []
    for resource in enabled_resources():
        field = PROVIDERS[resource.kind].body_limit if resource.kind in PROVIDERS else ""
        limit = int(resource.spec.get(field) or 0) if field and resource.key in serving else 0
        if not limit or limit >= needed:
            continue
        found.append(
            Finding(
                rule="proxy-refuses-uploads",
                subject=Kind.RESOURCE.of(resource.key),
                title=f"{resource.key} refuses uploads over {limit} MB",
                severity=Tone.SERIOUS,
                explanation="A larger upload fails before it reaches HQ.",
                evidence=(("Proxy lets through", f"{limit} MB"), ("HQ takes", f"{needed} MB")),
                remedies=(FindingRemedy(capability=ALLOW_UPLOADS, target=resource.key),),
            )
        )
    return tuple(found)


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "proxy-refuses-uploads",
        "Proxy refuses uploads",
        Tone.SERIOUS,
        _refusing,
        gathered=True,
        operator_action="Raise the proxy's largest request to what HQ takes.",
        no_help_reason="HQ cannot change a proxy it does not manage.",
        bars={Tone.SERIOUS: "a proxy in front of HQ refuses a request smaller than the largest HQ takes"},
    ),
)
