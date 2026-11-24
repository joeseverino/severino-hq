"""Findings about the tailnet's policy and settings."""

import json
from dataclasses import dataclass

from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_KIND, TAILNET_POLICY_KIND
from hq.domains.control_plane.providers import PROVIDERS

from . import trusted_networks
from .action_links import command_url
from .finding_model import (
    THEN_CHECK_AGAIN,
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    Said,
    per_connection,
)
from .finding_steps import Do
from .tailnet_loopback import GRANT_REACHES_ONLY_LOOPBACK, LoopbackGrant, LoopbackPort, loopback_grants
from .tone import Tone
from .ui import agreed, counted, counted_forms, ended


def _devices_join_without_approval(estate: FindingEstate) -> tuple[Finding, ...]:
    """Device approval is off: any valid auth key adds a device with no review."""

    return tuple(
        Finding(
            rule="devices-join-without-approval",
            subject=node.id,
            title="New devices join the tailnet without approval",
            severity=Tone.ATTENTION,
            explanation="Any valid auth key adds a device.",
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
            severity=Tone.NEUTRAL,
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
            "steps": (Do(self.step.format(names=joined)),),
        }


_EMPTY_GROUP = "Anyone added later gets this access."
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
    because=("Any device tagged later gets this access.", "Any device tagged later gets that access."),
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
        severity=Tone.ATTENTION,
        shown_as="DNS server",
        says=lambda _node, resolvers: {
            "title": (
                f"Tailnet DNS points at {', '.join(resolvers)}, which "
                f"{agreed(len(resolvers), 'is not a tailnet address', 'are not tailnet addresses')}"
            ),
            "explanation": "The DNS server sees every device as one.",
        },
    )


def _grant_reaches_only_loopback(estate: FindingEstate) -> tuple[Finding, ...]:
    """A grant opens a port on a machine where what HQ knows listens there binds only loopback.

    The port is open in the policy for nothing HQ knows of, and a policy test
    that accepts it keeps it so. What the card claims is only what HQ read.
    """

    grants = loopback_grants()
    if not grants:
        return ()
    providers = PROVIDERS[TAILNET_KIND].connection_providers
    return tuple(
        _loopback_finding(node, grants)
        for node in estate.nodes()
        if node.kind == "connection" and node.provider in providers
    )


def _loopback_finding(node, grants: tuple[LoopbackGrant, ...]) -> Finding:
    return Finding(
        rule=GRANT_REACHES_ONLY_LOOPBACK,
        subject=node.id,
        severity=Tone.NEUTRAL,
        title=_loopback_title(grants),
        explanation=agreed(sum(len(item.ports) for item in grants), "It reaches nothing.", "They reach nothing."),
        evidence=tuple(
            evidence
            for item in grants
            for evidence in (
                ("Grant", f"{', '.join(item.grant.get('src') or ())} to {', '.join(item.grant.get('dst') or ())}"),
                *(("Port", f"{item.machine} {port.entry}, {_listens(port)}") for port in item.ports),
            )
        ),
        steps=tuple(step for item in grants for step in _loopback_steps(item)),
    )


def _loopback_title(grants: tuple[LoopbackGrant, ...]) -> str:
    ports = [port for item in grants for port in item.ports]
    if len(grants) == 1 and len(ports) == 1:
        (item,), (port,) = grants, ports
        return f"A grant opens {port.entry} on {item.machine}, which {_listens(port)}"
    return f"{counted(len(ports), 'granted port is', 'granted ports are')} bound only to loopback"


def _listens(port: LoopbackPort) -> str:
    return "HQ serves only on loopback" if port.hq else "every container publishing it binds to loopback"


def _loopback_steps(item: LoopbackGrant) -> tuple[Do, ...]:
    """The grant without the ports, then each test that accepts them with them moved to deny."""

    whom = f"{', '.join(item.grant.get('src') or ())} to {', '.join(item.grant.get('dst') or ())}"
    fragment = item.fragment
    edit = (
        Do(
            f"In the Tailscale admin console, open Access controls and replace the grant from {whom} with this one.",
            value=json.dumps(fragment),
        )
        if fragment
        else Do(ended(f"In the Tailscale admin console, open Access controls and remove the grant from {whom}"))
    )
    tests = tuple(
        Do(
            "Replace the policy test that accepts the port with this one, which denies it.",
            value=json.dumps(test),
            notes=("Tailscale rejects a policy whose test fails, so the port cannot be opened again by mistake.",),
        )
        for test in item.tests
    )
    last = (*tests, Do(THEN_CHECK_AGAIN))
    return (edit, *last)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "tag-granted-to-nobody",
        "Tag no device has",
        Tone.NEUTRAL,
        _tag_granted_to_nobody,
        operator_action="Remove the tag from its rules, or tag its devices.",
        no_help_reason="HQ cannot tell whether the rule or the devices are wrong.",
        bars={Tone.NEUTRAL: "the policy grants a tag that no device carries"},
    ),
    FindingRule(
        "tailnet-dns-off-tailnet",
        "Tailnet DNS is not a tailnet address",
        Tone.ATTENTION,
        _tailnet_dns_off_tailnet,
        operator_action="In Tailscale's DNS settings, set the nameserver to the DNS server's tailnet address.",
        no_help_reason="HQ cannot change the tailnet's DNS settings.",
        bars={Tone.ATTENTION: "the tailnet's nameserver is not a tailnet address"},
    ),
    FindingRule(
        "devices-join-without-approval",
        "New devices join without approval",
        Tone.ATTENTION,
        _devices_join_without_approval,
        operator_action="In Tailscale's device management settings, turn on Device approval.",
        no_help_reason="HQ cannot change this setting.",
        bars={Tone.ATTENTION: "a new device can join the tailnet without anyone approving it"},
    ),
    FindingRule(
        "empty-group-granted",
        "Empty group still granted access",
        Tone.NEUTRAL,
        _empty_group_granted,
        operator_action="Remove the group from the policy, or add its members.",
        no_help_reason="HQ edits only a policy it manages.",
        bars={Tone.NEUTRAL: "the policy grants access to a group with no members"},
    ),
    FindingRule(
        GRANT_REACHES_ONLY_LOOPBACK,
        "Grant to a loopback port",
        Tone.NEUTRAL,
        _grant_reaches_only_loopback,
        gathered=True,
        operator_action="Remove the port from the grant, and deny it in its policy test.",
        no_help_reason="HQ cannot edit the tailnet policy.",
        bars={Tone.NEUTRAL: "a grant opens a port that only answers on the machine itself"},
    ),
    FindingRule(
        "trusted-wider-than-tailnet",
        "Trusted networks wider than the tailnet",
        Tone.NEUTRAL,
        trusted_networks.wider_than_tailnet,
        operator_action="Narrow HQ's trusted networks to the tailnet's addresses and routes, then restart HQ.",
        no_help_reason="HQ cannot change its own deploy settings.",
        bars={Tone.NEUTRAL: "HQ trusts addresses beyond the tailnet's"},
    ),
)
