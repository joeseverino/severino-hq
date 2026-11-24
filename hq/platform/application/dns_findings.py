"""Findings from what the AdGuard readings say: protection, filtering, upstreams, unused names.

Read off the facts ``topology`` puts on each AdGuard connection node from
``ObservationSpec.facts``. ``RULES`` declares each rule beside the detector
that decides it.
"""

from hq.domains.control_plane.observations.adguard import (
    FILTERING_OFF,
    NAME_UNUSED,
    PLAIN_UPSTREAM,
    PROTECTION_OFF,
    UNUSED_AFTER_HOURS,
)

from .entity_links import entity_link, node_link
from .finding_model import Finding, FindingEstate, FindingRule
from .tone import Tone
from .topology_model import TopologyNode
from .ui import counted


def _values(node: TopologyNode, key: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for name, value in node.facts if name == key and value))


def _connections(estate: FindingEstate):
    return (node for node in estate.nodes() if node.kind == "connection")


def _switched_off(estate: FindingEstate, fact: str, rule: str, setting: str, explanation: str) -> tuple[Finding, ...]:
    """A finding per connection that reports one AdGuard setting off, named for the setting."""

    return tuple(
        Finding(
            rule=rule,
            subject=node.id,
            title=f"AdGuard {setting.lower()} is off on {node.label}",
            severity=Tone.ATTENTION,
            explanation=explanation,
            evidence=((setting, "Off"),),
        )
        for node in _connections(estate)
        if _values(node, fact)
    )


def protection_off(estate: FindingEstate) -> tuple[Finding, ...]:
    return _switched_off(
        estate,
        PROTECTION_OFF,
        "dns-protection-off",
        "Protection",
        "Blocklists, safe browsing and client rules are skipped.",
    )


def filtering_off(estate: FindingEstate) -> tuple[Finding, ...]:
    return _switched_off(estate, FILTERING_OFF, "dns-filtering-off", "Filtering", "No blocklist applies to any lookup.")


def plain_upstream(estate: FindingEstate) -> tuple[Finding, ...]:
    found = []
    for node in _connections(estate):
        hosts = _values(node, PLAIN_UPSTREAM)
        if not hosts:
            continue
        found.append(
            Finding(
                rule="dns-plain-upstream",
                subject=node.id,
                title=(
                    f"AdGuard on {node.label} sends lookups to "
                    f"{', '.join(hosts) if len(hosts) <= 2 else counted(len(hosts), 'server')} "
                    "unencrypted"
                ),
                severity=Tone.NEUTRAL,
                explanation="Your provider can see every name looked up.",
                evidence=tuple(("Unencrypted server", host) for host in hosts),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def unused_names(estate: FindingEstate) -> tuple[Finding, ...]:
    """A name AdGuard rewrites that no device looked up over the window.

    On the service's node when HQ lists the name as a service, else on the
    connection that read the log.
    """

    services = {node.label: node.id for node in estate.nodes() if node.kind == "service"}
    found = [
        Finding(
            rule="dns-name-unused",
            subject=services.get(name, node.id),
            title=f"No device looked up {name}",
            severity=Tone.NEUTRAL,
            explanation="AdGuard still has a record for it.",
            evidence=(
                ("Name", entity_link("service", name)),
                ("Read from", node_link(node)),
                ("Query log covers", f"{UNUSED_AFTER_HOURS} hours or more"),
            ),
        )
        for node in _connections(estate)
        for name in _values(node, NAME_UNUSED)
    ]
    return tuple(sorted(found, key=lambda finding: finding.title))


_CANNOT_SWITCH_ON = "HQ cannot switch it on."

RULES: tuple[FindingRule, ...] = (
    FindingRule(
        name="dns-protection-off",
        title="DNS protection is off",
        severity=Tone.ATTENTION,
        detect=protection_off,
        operator_action="Turn protection back on in AdGuard.",
        no_help_reason=_CANNOT_SWITCH_ON,
        bars={Tone.ATTENTION: "AdGuard's protection is switched off"},
    ),
    FindingRule(
        name="dns-filtering-off",
        title="DNS filtering is off",
        severity=Tone.ATTENTION,
        detect=filtering_off,
        operator_action="Turn filtering on under Filters, DNS blocklists in AdGuard.",
        no_help_reason=_CANNOT_SWITCH_ON,
        bars={Tone.ATTENTION: "AdGuard's filtering is switched off"},
    ),
    FindingRule(
        name="dns-plain-upstream",
        title="DNS lookups sent unencrypted",
        severity=Tone.NEUTRAL,
        detect=plain_upstream,
        operator_action="Switch each to its DNS-over-TLS or DNS-over-HTTPS address in AdGuard.",
        no_help_reason="HQ cannot change AdGuard's upstream servers.",
        bars={Tone.NEUTRAL: "an upstream server takes lookups unencrypted"},
    ),
    FindingRule(
        name="dns-name-unused",
        title="Unused name",
        severity=Tone.NEUTRAL,
        detect=unused_names,
        operator_action="Remove the service, or check its devices use AdGuard for DNS.",
        no_help_reason="HQ cannot tell whether anything still needs it.",
        bars={Tone.NEUTRAL: "nothing has looked up a name AdGuard answers"},
    ),
)
