"""Findings from what the AdGuard readings say: protection, filtering, upstreams, unused names.

Read off the facts ``topology`` puts on each AdGuard connection node from
``ObservationSpec.facts``. Detectors return a ``Finding``'s fields; ``RULES``
declares each rule beside the detector that decides it.
"""

from __future__ import annotations

from typing import Any

from hq.domains.control_plane.observations.adguard import (
    FILTERING_OFF,
    NAME_UNUSED,
    PLAIN_UPSTREAM,
    PROTECTION_OFF,
)

from .finding_model import FindingRule, built_findings
from .ui import counted


def _values(node: Any, key: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for name, value in node.facts if name == key and value))


def _connections(estate: Any):
    return (node for node in estate.nodes() if node.kind == "connection")


def protection_off(estate: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(
            rule="dns-protection-off",
            subject=node.id,
            title=f"{node.label} answers DNS with protection off",
            severity="attention",
            explanation=(
                "AdGuard resolves every query without applying blocklists, "
                "safe browsing or client rules."
            ),
            evidence=(("Protection", "Off"),),
        )
        for node in _connections(estate)
        if _values(node, PROTECTION_OFF)
    )


def filtering_off(estate: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(
            rule="dns-filtering-off",
            subject=node.id,
            title=f"{node.label} does not filter DNS",
            severity="attention",
            explanation="Filtering is off in AdGuard, so no blocklist applies to any query.",
            evidence=(("Filtering", "Off"),),
        )
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
            dict(
                rule="dns-plain-upstream",
                subject=node.id,
                title=(
                    f"{node.label} forwards queries over plain DNS to "
                    f"{counted(len(hosts), 'upstream')}"
                ),
                severity="neutral",
                explanation=(
                    "These upstreams are off the site and are queried without "
                    "encryption, so the network between can read every name looked up."
                ),
                evidence=tuple(("Plain upstream", host) for host in hosts),
            )
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
        for name in _values(node, NAME_UNUSED):
            found.append(
                dict(
                    rule="dns-name-unused",
                    subject=services.get(name, node.id),
                    title=f"No device looked up {name}",
                    severity="neutral",
                    explanation=(
                        f"AdGuard rewrites {name}, and its query log shows no lookup "
                        "of it over the span it covers, up to a day."
                    ),
                    evidence=(("Name", name), ("Query log", node.label)),
                )
            )
    return tuple(sorted(found, key=lambda finding: finding["title"]))


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        name="dns-protection-off",
        title="DNS protection off",
        severity="attention",
        detect=lambda estate, detect=protection_off: built_findings(detect(estate)),
        operator_action="Turn protection back on from the AdGuard dashboard.",
        no_help_reason=(
            "HQ reads AdGuard's protection state but has no capability that changes it."
        ),
    ),
    FindingRule(
        name="dns-filtering-off",
        title="DNS filtering off",
        severity="attention",
        detect=lambda estate, detect=filtering_off: built_findings(detect(estate)),
        operator_action="Turn filtering on under Filters, DNS blocklists in AdGuard.",
        no_help_reason=(
            "HQ reads AdGuard's filtering state but has no capability that changes it."
        ),
    ),
    FindingRule(
        name="dns-plain-upstream",
        title="Upstream DNS unencrypted",
        severity="neutral",
        detect=lambda estate, detect=plain_upstream: built_findings(detect(estate)),
        operator_action=(
            "Replace each plain upstream with its provider's DNS-over-TLS or DNS-over-HTTPS "
            "address under Settings, DNS settings in AdGuard, if queries should be encrypted."
        ),
        no_help_reason=(
            "Which encrypted upstream to use is your choice, and no HQ capability writes AdGuard's upstreams."
        ),
    ),
    FindingRule(
        name="dns-name-unused",
        title="Name nobody looks up",
        severity="neutral",
        detect=lambda estate, detect=unused_names: built_findings(detect(estate)),
        operator_action=(
            "Remove the service and its rewrite if nothing uses it; otherwise point the "
            "device that should use it at AdGuard for DNS."
        ),
        no_help_reason=(
            "Whether anything still needs the name is something only you know."
        ),
    ),
)
