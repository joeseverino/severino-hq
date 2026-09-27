"""Trusted networks that admit a whole Tailscale range the tailnet only uses part of.

Informational: trust is configuration, and HQ never narrows it itself. The rule
returns a ``Finding``'s fields; ``findings`` builds the finding.
"""

from __future__ import annotations

from ipaddress import ip_network
from typing import Any

from django.conf import settings

from core.network import parse_ip

from .credential_findings import OperatorStep
from .reach import TAILNET
from .ui import counted


def wider_than_tailnet(estate: Any) -> tuple[dict[str, Any], ...]:
    """Trusted networks admit a whole Tailscale range; the tailnet uses less."""

    wide = _wide_trust()
    if not wide:
        return ()
    found: list[dict[str, Any]] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        addresses = tuple(
            address
            for key, address in node.facts
            if key == "tailnet-address" and _within(address, wide)
        )
        routes = tuple(v for k, v in node.facts if k == "tailnet-route" and v)
        if not addresses:
            continue
        uses = counted(len(addresses), "device address", "device addresses")
        if routes:
            uses += f" and {counted(len(routes), 'subnet route', 'subnet routes')}"
        found.append(
            {
                "rule": "trusted-wider-than-tailnet",
                "subject": node.id,
                "title": f"HQ trusts all of {', '.join(map(str, wide))}; the tailnet uses {uses}",
                "severity": "neutral",
                "explanation": (
                    "SEVERINO_TRUSTED_NETWORKS admits every address in the range. "
                    "Narrowing it to what the tailnet uses is an operator's "
                    "decision; HQ does not change it."
                ),
                "evidence": (
                    *(("Trusted", str(network)) for network in wide),
                    *(("Device address", address) for address in addresses),
                    *(("Subnet route", route) for route in routes),
                ),
                "steps": (
                    OperatorStep(
                        label="Narrow the trusted networks, then restart HQ",
                        command=(
                            "SEVERINO_TRUSTED_NETWORKS="
                            f"{narrowed(wide, addresses, routes)}"
                        ),
                        notes=("A device that joins later is refused until it is added.",),
                    ),
                ),
            }
        )
    return tuple(sorted(found, key=lambda finding: finding["title"]))


def _network(cidr: Any):
    """A trusted network entry as a network, or None when it is not one."""

    try:
        return ip_network(str(cidr).strip(), strict=False)
    except ValueError:
        return None


def _wide_trust() -> tuple:
    """Trusted networks that hold a whole Tailscale range, either family."""

    return tuple(
        network
        for cidr in settings.SEVERINO_TRUSTED_NETWORKS
        if (network := _network(cidr)) is not None
        and any(
            network.version == tailnet.version and network.supernet_of(tailnet)
            for tailnet in TAILNET
        )
    )


def _within(address: str, networks) -> bool:
    found = parse_ip(address)
    return found is not None and any(_within_any((found,), network) for network in networks)


def _within_any(hosts, network) -> bool:
    return any(host.version == network.version and host in network for host in hosts)


def narrowed(wide, addresses, routes) -> str:
    """The trusted networks with each wide range replaced by the device addresses
    in it, one host each (/32 or /128), then the subnet routes. A wide range no
    device address falls in is kept: nothing read says what narrows it."""

    hosts = sorted(
        (parse_ip(address) for address in addresses),
        key=lambda found: (found.version, found),
    )
    kept = [
        str(cidr).strip()
        for cidr in settings.SEVERINO_TRUSTED_NETWORKS
        if (network := _network(cidr)) is not None
        and (network not in wide or not _within_any(hosts, network))
    ]
    used = [str(ip_network(host)) for host in hosts] + sorted(routes)
    return ",".join(dict.fromkeys([*kept, *used]))
