"""Findings about the tailnet's policy and settings."""

from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_POLICY_KIND

from . import trusted_networks
from .action_links import command_url
from .finding_model import (
    OperatorStep,
    Finding,
    Remedy,
    FindingEstate,
    built_findings,
    fact_values,
    FindingRule,
)
from .ui import counted


def _devices_join_without_approval(estate: FindingEstate) -> tuple[Finding, ...]:
    """Device approval is off: any valid auth key adds a device with no review."""

    return tuple(
        Finding(
            rule="devices-join-without-approval",
            subject=node.id,
            title="New devices join the tailnet without approval",
            severity="attention",
            explanation="Any valid auth key adds a device with no review.",
            evidence=(("Device approval", "Off"),),
        )
        for node in estate.nodes()
        if node.kind == "connection"
        and any(key == "devices-join-unapproved" for key, _ in node.facts)
    )


def _empty_group_granted(estate: FindingEstate) -> tuple[Finding, ...]:
    """A group with no members that a grant or shell rule still names."""

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        empty = fact_values(node, "empty-group-granted")
        if not empty:
            continue
        found.append(
            Finding(
                rule="empty-group-granted",
                subject=node.id,
                title=(
                    f"{empty[0]} has no members but is still granted access"
                    if len(empty) == 1
                    else f"{counted(len(empty), 'group has', 'groups have')} no "
                    "members but are still granted access"
                ),
                severity="neutral",
                explanation=(
                    "A rule for an empty group lets nobody in. Remove the group "
                    "from the policy, or add the members it was meant for."
                ),
                evidence=tuple(("Empty group", name) for name in empty),
                remedies=_policy_remedy(
                    estate,
                    "tailnet.policy.remove_empty_groups",
                    "Remove empty groups",
                ),
                steps=(
                    OperatorStep(
                        label=f"Remove {', '.join(empty)} from the policy's groups and "
                        "from every rule that names them, or add their members."
                    ),
                ),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _tag_granted_to_nobody(estate: FindingEstate) -> tuple[Finding, ...]:
    """A tag a grant names that no device carries: the policy and the devices disagree."""

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        tags = fact_values(node, "tag-granted-to-nobody")
        if not tags:
            continue
        found.append(
            Finding(
                rule="tag-granted-to-nobody",
                subject=node.id,
                title=(
                    f"{tags[0]} is granted access but no device has it"
                    if len(tags) == 1
                    else f"{counted(len(tags), 'tag is', 'tags are')} granted access but no device has them"
                ),
                severity="neutral",
                explanation=(
                    (
                        f"No device has {tags[0]}, so this rule lets nothing in now. "
                        "Any device given the tag later gets this access."
                        if len(tags) == 1
                        else "No device has these tags, so their rules let nothing in now. "
                        "Any device given one of them later gets that access."
                    )
                ),
                evidence=tuple(("Tag", tag) for tag in tags),
                remedies=_policy_remedy(estate, "infrastructure.resource.update", "Change what HQ expects"),
                steps=(
                    OperatorStep(
                        label=f"Remove {', '.join(tags)} from the rules that name them, or tag the devices they were meant for."
                    ),
                ),
            )
        )
    return tuple(found)


def _policy_remedy(estate: FindingEstate, capability: str, label: str) -> tuple[Remedy, ...]:
    """A policy amendment, offered when a tailnet policy is declared to amend.

    The capability re-derives the change from the declaration and writes it
    through the gated policy kind, so a person still consents.
    """

    policy = next(
        (
            node
            for node in estate.nodes()
            if node.kind == "resource" and node.kind_key == TAILNET_POLICY_KIND
        ),
        None,
    )
    if policy is None:
        return ()
    url = command_url(capability, policy.label) if policy is not None else ""
    if not url:
        return ()
    return (
        Remedy(
            capability=capability,
            target=policy.label,
            label=label,
            effect="",
            url=url,
        ),
    )


def _tailnet_dns_off_tailnet(estate: FindingEstate) -> tuple[Finding, ...]:
    """A tailnet resolver that is not the address of any tailnet device.

    Clients then reach it through a subnet router or the internet, and it sees
    one source address for all of them rather than each device.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        resolvers = tuple(
            value
            for key, value in node.facts
            if key == "tailnet-dns-off-tailnet" and value
        )
        if not resolvers:
            continue
        found.append(
            Finding(
                rule="tailnet-dns-off-tailnet",
                subject=node.id,
                title=(
                    f"Tailnet DNS points at {', '.join(resolvers)}, which "
                    f"{'is not a tailnet address' if len(resolvers) == 1 else 'are not tailnet addresses'}"
                ),
                severity="attention",
                explanation=(
                    "Devices reach it through a subnet router or the internet, so "
                    "the DNS server sees them all as one device."
                ),
                evidence=tuple(("DNS server", resolver) for resolver in resolvers),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _trusted_wider_than_tailnet(estate: FindingEstate) -> tuple[Finding, ...]:
    return built_findings(trusted_networks.wider_than_tailnet(estate))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "tag-granted-to-nobody",
        "A tag in the policy that no device has",
        "neutral",
        _tag_granted_to_nobody,
        operator_action="Remove the tag from the rules that name it, or tag the devices it was meant for.",
        no_help_reason="HQ cannot tell whether the rule or the devices are wrong.",
    ),
    FindingRule(
        "tailnet-dns-off-tailnet",
        "Tailnet DNS is not a tailnet address",
        "attention",
        _tailnet_dns_off_tailnet,
        operator_action=(
            "In the Tailscale admin console, open DNS and set the nameserver to the DNS server's tailnet address."
        ),
        no_help_reason=(
            "HQ cannot change the tailnet's DNS settings."
        ),
    ),
    FindingRule(
        "devices-join-without-approval",
        "New devices join without approval",
        "attention",
        _devices_join_without_approval,
        operator_action=(
            "In the Tailscale admin console, open Settings, then Device management, and turn on Device approval."
        ),
        no_help_reason=(
            "HQ cannot change this setting."
        ),
    ),
    FindingRule(
        "empty-group-granted",
        "Empty group still granted access",
        "neutral",
        _empty_group_granted,
        operator_action=(
            "Remove the group from the policy's groups and from every rule that names it, or add its members."
        ),
        no_help_reason=(
            "HQ can only edit the policy once the policy is managed in HQ."
        ),
    ),
    FindingRule(
        "trusted-wider-than-tailnet",
        "Trusted networks wider than the tailnet",
        "neutral",
        _trusted_wider_than_tailnet,
        operator_action=(
            "Set SEVERINO_TRUSTED_NETWORKS to the addresses and routes the tailnet uses, then restart HQ."
        ),
        no_help_reason=(
            "HQ cannot change its own deploy settings."
        ),
    ),
)
