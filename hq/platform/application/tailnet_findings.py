"""Findings about the tailnet's policy and settings."""

from dataclasses import dataclass

from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_POLICY_KIND

from . import trusted_networks
from .action_links import command_url
from .finding_model import (
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    OperatorStep,
    Said,
    built_findings,
    per_connection,
)
from .ui import counted, counted_forms


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
        if node.kind == "connection" and any(key == "devices-join-unapproved" for key, _ in node.facts)
    )


@dataclass(frozen=True, slots=True)
class _GrantedToNobody:
    """A name the policy grants access to that lets nobody in, and how its finding words it.

    ``{names}`` in a template is the names the connection reported, joined. A
    title for several is their count, said with ``counted_as``, then ``many``.
    ``because`` explains one name, then several.
    """

    rule: str
    shown_as: str
    # The policy amendment offered.
    capability: str
    one: str
    counted_as: tuple[str, str]
    many: str
    because: tuple[str, str]
    step: str

    def __post_init__(self) -> None:
        counted_forms(*self.counted_as)

    def findings(self, estate: FindingEstate) -> tuple[Finding, ...]:
        remedies = _policy_remedy(estate, self.capability)
        return per_connection(
            estate,
            self.rule,
            rule=self.rule,
            severity="neutral",
            shown_as=self.shown_as,
            says=lambda _node, names: self._said(names, remedies),
        )

    def _said(self, names: tuple[str, ...], remedies: tuple[FindingRemedy, ...]) -> Said:
        one = len(names) == 1
        joined = ", ".join(names)
        return {
            "title": self.one.format(names=joined) if one else f"{counted(len(names), *self.counted_as)} {self.many}",
            "explanation": self.because[0 if one else 1].format(names=joined),
            "remedies": remedies,
            "steps": (OperatorStep(label=self.step.format(names=joined)),),
        }


_EMPTY_GROUP = (
    "A rule for an empty group lets nobody in. Remove the group from the policy, or add the members it was meant for."
)
_EMPTY_GROUP_GRANTED = _GrantedToNobody(
    rule="empty-group-granted",
    shown_as="Empty group",
    capability="tailnet.policy.remove_empty_groups",
    one="{names} has no members but is still granted access",
    counted_as=("group has", "groups have"),
    many="no members but are still granted access",
    because=(_EMPTY_GROUP, _EMPTY_GROUP),
    step="Remove {names} from the policy's groups and from every rule that names them, or add their members.",
)
_TAG_GRANTED_TO_NOBODY = _GrantedToNobody(
    rule="tag-granted-to-nobody",
    shown_as="Tag",
    capability="infrastructure.resource.update",
    one="{names} is granted access but no device has it",
    counted_as=("tag is", "tags are"),
    many="granted access but no device has them",
    because=(
        "No device has {names}, so this rule lets nothing in now. Any device given the tag later gets this access.",
        (
            "No device has these tags, so their rules let nothing in now. "
            "Any device given one of them later gets that access."
        ),
    ),
    step="Remove {names} from the rules that name them, or tag the devices they were meant for.",
)


def _empty_group_granted(estate: FindingEstate) -> tuple[Finding, ...]:
    """A group with no members that a grant or shell rule still names."""

    return _EMPTY_GROUP_GRANTED.findings(estate)


def _tag_granted_to_nobody(estate: FindingEstate) -> tuple[Finding, ...]:
    """A tag a grant names that no device carries: the policy and the devices disagree."""

    return _TAG_GRANTED_TO_NOBODY.findings(estate)


def _policy_remedy(estate: FindingEstate, capability: str) -> tuple[FindingRemedy, ...]:
    """A policy amendment, offered when a tailnet policy is declared to amend.

    The capability re-derives the change from the declaration and writes it
    through the gated policy kind, so a person still consents.
    """

    policy = next(
        (node for node in estate.nodes() if node.kind == "resource" and node.kind_key == TAILNET_POLICY_KIND),
        None,
    )
    if policy is None:
        return ()
    url = command_url(capability, policy.label)
    if not url:
        return ()
    return (FindingRemedy(capability=capability, target=policy.label, url=url),)


def _tailnet_dns_off_tailnet(estate: FindingEstate) -> tuple[Finding, ...]:
    """A tailnet resolver that is not the address of any tailnet device.

    Clients then reach it through a subnet router or the internet, and it sees
    one source address for all of them rather than each device.
    """

    return per_connection(
        estate,
        "tailnet-dns-off-tailnet",
        rule="tailnet-dns-off-tailnet",
        severity="attention",
        shown_as="DNS server",
        says=lambda _node, resolvers: {
            "title": (
                f"Tailnet DNS points at {', '.join(resolvers)}, which "
                f"{'is not a tailnet address' if len(resolvers) == 1 else 'are not tailnet addresses'}"
            ),
            "explanation": (
                "Devices reach it through a subnet router or the internet, so the DNS server sees them all as one device."
            ),
        },
    )


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
        no_help_reason=("HQ cannot change the tailnet's DNS settings."),
    ),
    FindingRule(
        "devices-join-without-approval",
        "New devices join without approval",
        "attention",
        _devices_join_without_approval,
        operator_action=(
            "In the Tailscale admin console, open Settings, then Device management, and turn on Device approval."
        ),
        no_help_reason=("HQ cannot change this setting."),
    ),
    FindingRule(
        "empty-group-granted",
        "Empty group still granted access",
        "neutral",
        _empty_group_granted,
        operator_action=(
            "Remove the group from the policy's groups and from every rule that names it, or add its members."
        ),
        no_help_reason=("HQ can only edit the policy once the policy is managed in HQ."),
    ),
    FindingRule(
        "trusted-wider-than-tailnet",
        "Trusted networks wider than the tailnet",
        "neutral",
        _trusted_wider_than_tailnet,
        operator_action=(
            "Narrow the trusted networks in HQ's settings to the addresses and routes the tailnet uses, then restart HQ."
        ),
        no_help_reason=("HQ cannot change its own deploy settings."),
    ),
)
