"""What HQ can put in front of a name the internet reaches without asking."""

from hq.domains.control_plane.providers import PROVIDERS

from .action_links import command_action, command_url
from .container_attention import UPDATE_CAPABILITY
from .workflow_contracts import ActionLink


def gate_links(service) -> tuple[ActionLink, ...]:
    """The declarations HQ can put a gate on for this name: any ingress whose
    provider states an ingress policy, updated through its own command."""

    found = []
    for facet in service.facets:
        for claim in facet.claims:
            provider = PROVIDERS.get(claim.kind)
            if provider is None or provider.ingress_policy is None:
                continue
            found.append(
                command_action(
                    "gate",
                    UPDATE_CAPABILITY,
                    command_url(UPDATE_CAPABILITY, claim.resource_key),
                    target=claim.resource_key,
                    intent=f"Put an access list in front of {claim.resource_key}",
                    reason="Set its access list, so the proxy admits only whom the list allows.",
                )
            )
    return tuple(found)
