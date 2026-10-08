"""Findings from what the AdGuard readings say: protection, filtering, upstreams, unused names.

Read off the facts ``topology`` puts on each AdGuard connection node from
``ObservationSpec.facts``. Detectors return a ``Finding``'s fields; ``RULES``
declares each rule beside the detector that decides it.
"""

from typing import Any

from hq.domains.control_plane.observations.adguard import (
    FILTERING_OFF,
    NAME_UNUSED,
    PLAIN_UPSTREAM,
    PROTECTION_OFF,
    UNUSED_AFTER_HOURS,
)

from .finding_model import FindingRule, built_findings
from .ui import counted


def _values(node: Any, key: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for name, value in node.facts if name == key and value))


def _connections(estate: Any):
    return (node for node in estate.nodes() if node.kind == "connection")


def protection_off(estate: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "rule": "dns-protection-off",
            "subject": node.id,
            "title": f"AdGuard protection is off on {node.label}",
            "severity": "attention",
            "explanation": ("AdGuard is answering every lookup without blocklists, safe browsing or client rules."),
            "evidence": (("Protection", "Off"),),
        }
        for node in _connections(estate)
        if _values(node, PROTECTION_OFF)
    )


def filtering_off(estate: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "rule": "dns-filtering-off",
            "subject": node.id,
            "title": f"AdGuard filtering is off on {node.label}",
            "severity": "attention",
            "explanation": "No blocklist applies to any lookup.",
            "evidence": (("Filtering", "Off"),),
        }
        for node in _connections(estate)
        if _values(node, FILTERING_OFF)
    )


def plain_upstream(estate: Any) -> tuple[dict[str, Any], ...]:
    found = []
    for node in _connections(estate):
        hosts = _values(node, PLAIN_UPSTREAM)
        if not hosts:
            continue
        found.append(
            {
                "rule": "dns-plain-upstream",
                "subject": node.id,
                "title": (
                    f"AdGuard on {node.label} sends lookups to "
                    f"{', '.join(hosts) if len(hosts) <= 2 else counted(len(hosts), 'server')} "
                    "unencrypted"
                ),
                "severity": "neutral",
                "explanation": (
                    "Your internet provider, and anyone else on the way, can see every name your devices look up."
                ),
                "evidence": tuple(("Unencrypted server", host) for host in hosts),
            }
        )
    return tuple(sorted(found, key=lambda finding: finding["title"]))


def unused_names(estate: Any) -> tuple[dict[str, Any], ...]:
    """A name AdGuard rewrites that no device looked up over the window.

    On the service's node when HQ lists the name as a service, else on the
    connection that read the log.
    """

    services = {node.label: node.id for node in estate.nodes() if node.kind == "service"}
    found = []
    for node in _connections(estate):
        found.extend(
            {
                "rule": "dns-name-unused",
                "subject": services.get(name, node.id),
                "title": f"No device looked up {name}",
                "severity": "neutral",
                "explanation": (
                    "AdGuard has a record for it, and no device asked for it in "
                    f"the time AdGuard's query log covers, {UNUSED_AFTER_HOURS} hours or more."
                ),
                "evidence": (("Name", name), ("Read from", node.label)),
            }
            for name in _values(node, NAME_UNUSED)
        )
    return tuple(sorted(found, key=lambda finding: finding["title"]))


_CANNOT_SWITCH_ON = "HQ cannot switch it on."

RULES: tuple[FindingRule, ...] = (
    FindingRule(
        name="dns-protection-off",
        title="DNS protection is off",
        severity="attention",
        detect=lambda estate, detect=protection_off: built_findings(detect(estate)),
        operator_action="Turn protection back on in AdGuard.",
        no_help_reason=_CANNOT_SWITCH_ON,
    ),
    FindingRule(
        name="dns-filtering-off",
        title="DNS filtering is off",
        severity="attention",
        detect=lambda estate, detect=filtering_off: built_findings(detect(estate)),
        operator_action="Turn filtering on under Filters, DNS blocklists in AdGuard.",
        no_help_reason=_CANNOT_SWITCH_ON,
    ),
    FindingRule(
        name="dns-plain-upstream",
        title="DNS lookups sent unencrypted",
        severity="neutral",
        detect=lambda estate, detect=plain_upstream: built_findings(detect(estate)),
        operator_action=(
            "Replace each one with its DNS-over-TLS or DNS-over-HTTPS address under Settings, DNS settings in AdGuard."
        ),
        no_help_reason="HQ cannot change AdGuard's upstream servers.",
    ),
    FindingRule(
        name="dns-name-unused",
        title="A name nothing looks up",
        severity="neutral",
        detect=lambda estate, detect=unused_names: built_findings(detect(estate)),
        operator_action=(
            "If nothing uses it, remove the service. If something should, check that device uses AdGuard for DNS."
        ),
        no_help_reason="HQ cannot tell whether anything still needs it.",
    ),
)
