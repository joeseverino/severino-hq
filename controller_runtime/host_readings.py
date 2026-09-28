"""Readings of the host the controller runs on: its firewall and its perimeter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from typing import Any

from control_plane.provider_adapters.contracts import ProviderError
from .handlers import reads
from . import commands, connection_env, portainer


# Whether the host firewall requires HQ's port to be reached over the tailnet
# interface. Handed over on the same terms as the two above: root reads the
# ruleset and mounts one distilled answer, because the ruleset itself is a map
# of every way into the machine and this process holds every provider
# credential. Absent on a host that does not run this firewall, which is a
# reading nothing observed rather than a firewall reported open.
HOST_FIREWALL = os.environ.get("SEVERINO_HOST_FIREWALL", "")


@reads("host.firewall")
def list_host_firewall() -> list[dict[str, Any]]:
    """Whether the packet had to arrive on the tailnet, or merely to claim it.

    The address a request comes from is a field the sender writes. The interface
    it arrived on is not, so a firewall that accepts HQ's port only from the
    tailnet interface is making a stronger statement than one that reads the
    source address, and it is the statement the request inspector's "the
    address is on the tailnet" line rests on.

    Read by root and handed over as one distilled answer; see HOST_FIREWALL.
    """

    # Not caught, for the reason given on list_tailnet_policy: a raising
    # collector is recorded as unreachable and carries its reason, where an
    # empty list is indistinguishable from a host whose firewall says nothing.
    # With no reading mounted the sweep reports the kind as not connected and
    # does not call this; the guard covers a direct call.
    if not HOST_FIREWALL:
        raise ProviderError(
            "No host firewall reading was mounted; this host does not report one."
        )
    reading = json.loads(Path(HOST_FIREWALL).read_text(encoding="utf-8"))
    return [reading]


def _answers_from_here(address: str, port: int, timeout: float = 3.0) -> bool:
    """Whether a TCP connection to this address and port is accepted.

    Asked from the machine the controller runs on, which reaches a public
    address the way anybody else would. That is the whole point: a firewall is
    a claim about what happens to a packet, and the only way to know is to send
    one.
    """

    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


@reads("host.perimeter")
def list_host_perimeter() -> list[dict[str, Any]]:
    """What each edge relies on to stay shut, and whether it actually is.

    Two halves, because one of them cannot be read without privileges this
    controller deliberately does not have. The machine reports the state of its
    firewall unit (a unit can be enabled and dead, and the difference has no
    symptom until something arrives) and reports its own public addresses.
    What is behind that firewall is then answered from here, by connecting to
    those addresses on the ports that machine's own containers publish.

    Nothing about which ports to try is written down. They come from the
    container inventory, so a machine that starts publishing something new is
    checked on it without anybody remembering to say so, plus the SSH ports,
    which must answer only on the tailnet. An empty list checked nothing, and
    HQ says so rather than reading it as shut.
    """

    found: list[dict[str, Any]] = []
    for connection_ref in connection_env.connection_refs_for_role("caddy"):
        reading = json.loads(commands._ssh(connection_ref, "perimeter") or b"{}")
        addresses = [
            address.strip()
            for address in str(reading.get("public_addresses", "")).split(",")
            if address.strip()
        ]
        transport = connection_env._transport(connection_ref)
        ports = sorted(
            _published_ports_at(connection_ref, {transport["host"], *addresses})
            | {_SSH_PORT, transport["port"]}
        )
        answered = sorted(
            {
                port
                for address in addresses
                for port in ports
                if _answers_from_here(address, port)
            }
        )
        found.append(
            {
                "record": "perimeter",
                "connection_ref": connection_ref,
                "firewall_unit": str(reading.get("firewall_unit", "unknown")),
                "public_addresses": addresses,
                "ports_checked": ports,
                "answered_publicly": answered,
                "read_at": str(reading.get("read_at", "")),
            }
        )
    return found


_SSH_PORT = 22


def _published_ports_at(connection_ref: str, addresses: set[str]) -> set[int]:
    """Ports the containers on one machine publish, as the sweep found them.

    A container is on the machine when its environment carries the
    connection's name or answers at one of the machine's addresses: an SSH
    item and a Portainer environment name one machine differently, and the
    address is what both agree on.

    Empty where the machine is not described, which reads as nothing to check
    rather than as nothing published: the same distinction the container
    reading draws about host networking.
    """

    ports: set[int] = set()
    try:
        containers = portainer.list_portainer_containers()
    except (ProviderError, OSError, ValueError, KeyError):
        return ports
    at = {connection_ref, *addresses} - {""}
    for container in containers:
        if not {
            str(container.get("host", "")),
            str(container.get("host_address", "")),
        } & at:
            continue
        ports.update(
            int(port)
            for port in container.get("ports") or ()
            if str(port).isdigit() and 0 < int(port) < 65536
        )
    return ports
