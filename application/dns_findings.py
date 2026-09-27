"""Findings from what the AdGuard readings say: protection, filtering, upstreams, unused names.

Read off the facts ``topology`` puts on each AdGuard connection node from
``ObservationSpec.facts``. Detectors return a ``Finding``'s fields and ``RULES``
a ``FindingRule``'s; ``findings`` builds both, so this module does not import it.
"""

from __future__ import annotations

from typing import Any

from control_plane.observations.adguard import (
    FILTERING_OFF,
    NAME_UNUSED,
    PLAIN_UPSTREAM,
    PROTECTION_OFF,
)

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


RULES: tuple[dict[str, Any], ...] = (
    dict(
        name="dns-protection-off",
        title="DNS protection off",
        severity="attention",
        detect=protection_off,
        operator_action="Turn protection back on from the AdGuard dashboard.",
    ),
    dict(
        name="dns-filtering-off",
        title="DNS filtering off",
        severity="attention",
        detect=filtering_off,
        operator_action="Turn filtering on under Filters, DNS blocklists in AdGuard.",
    ),
    dict(
        name="dns-plain-upstream",
        title="Upstream DNS unencrypted",
        severity="neutral",
        detect=plain_upstream,
        operator_action=(
            "Replace each plain upstream with its provider's DNS-over-TLS or DNS-over-HTTPS "
            "address under Settings, DNS settings in AdGuard, if queries should be encrypted."
        ),
    ),
    dict(
        name="dns-name-unused",
        title="Name nobody looks up",
        severity="neutral",
        detect=unused_names,
        operator_action=(
            "Remove the service and its rewrite if nothing uses it; otherwise point the "
            "device that should use it at AdGuard for DNS."
        ),
    ),
)
