"""How HQ reaches each connection: over which network, to which machine, by which path.

Joined from readings HQ already holds: the connection's endpoint, the addresses
HQ's DNS readings see its name answering at, the machine catalogue, and the
tailnet peering the machine page shows (``Presence.peer_path``). Nothing is
probed and nothing is resolved live.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from hq.domains.control_plane.names import normalized_hostname

from .entity_links import EntityLink, entity_link
from .facts import stored_snapshots
from .reach import network_of

NETWORK_LABELS = {
    "loopback": "Reached locally",
    "tailnet": "Reached over the tailnet",
    "network": "Reached on your network",
    "public": "Reached over the internet",
}
# RFC 6761 and RFC 8375 names no public resolver answers.
_SPECIAL_USE = (".local", ".home.arpa", ".internal", ".localhost")


@dataclass(frozen=True, slots=True)
class ConnectionReach:
    """The network a connection is reached over, its machine, and the peering."""

    network: str
    host: str
    address: str = ""
    machine: EntityLink | None = None
    # The machine's tailnet presence, as its own page shows it.
    presence: Any = None

    @property
    def label(self) -> str:
        return NETWORK_LABELS.get(self.network, "")

    @property
    def peer_path(self) -> str:
        """``direct``, ``relayed`` or ``negotiating`` over the tailnet; else ""."""

        if self.network != "tailnet" or self.presence is None:
            return ""
        return self.presence.peer_path

    @property
    def peering(self) -> str:
        path = self.peer_path
        if path == "relayed" and self.presence.relay:
            return f"relayed via {self.presence.relay}"
        return path

    @property
    def machine_word(self) -> str:
        """The word between the network and its machine: "on" where HQ runs, else "at"."""

        return "on" if self.network == "loopback" else "at"

    @property
    def relay(self) -> str:
        """How the tailnet leg is carried, said only when it is not direct."""

        return "" if self.peer_path == "direct" else self.peering

    @property
    def summary(self) -> str:
        """One line: network, machine, and the relay when there is one."""

        if not self.label:
            return ""
        line = (
            f"{self.label} {self.machine_word} {self.machine.label}"
            if self.machine
            else self.label
        )
        return f"{line}, {self.relay}" if self.relay else line

    def as_dict(self) -> dict[str, Any]:
        presence = self.presence if self.peer_path else None
        return {
            "network": self.network or None,
            "label": self.label or None,
            "summary": self.summary or None,
            "host": self.host,
            "address": self.address or None,
            "machine": (
                {"name": self.machine.label, "url": self.machine.url} if self.machine else None
            ),
            "peer_path": self.peer_path or None,
            "direct_endpoint": presence.direct_endpoint or None if presence else None,
            "relay": presence.relay or None if presence else None,
            "handshake": presence.last_handshake or None if presence else None,
            "rx_bytes": presence.rx_bytes if presence else None,
            "tx_bytes": presence.tx_bytes if presence else None,
        }


def _named_network(host: str) -> str:
    """The network a name no HQ reading resolves is on, by the name alone."""

    if "." not in host or host.endswith(_SPECIAL_USE):
        return "network"
    return "public"


def connection_reach(connections: Iterable[tuple[str, str]]) -> dict[str, ConnectionReach]:
    """``connection_ref -> ConnectionReach`` for each ``(ref, endpoint)`` with a host."""

    from .connections import machines_once
    from .locate import host_of, machines_index, observed_answers

    catalog = machines_once()
    known = {item.name.lower(): item for item in catalog}
    for item in catalog:
        for alias in item.aliases:
            known.setdefault(alias.lower(), item)
    index = machines_index()
    # From the one read of every reading the connections page shares.
    answers = observed_answers(row for rows in stored_snapshots().values() for row in rows)
    found: dict[str, ConnectionReach] = {}
    for ref, endpoint in connections:
        host = host_of(endpoint)
        if not ref or not host:
            continue
        address = host if network_of(host) else next(
            iter(answers.get(normalized_hostname(host), ())), ""
        )
        placed = index.resolve(address) if address else index.named(host)
        machine = known.get(placed.lower()) if placed else None
        network = network_of(address) if address else _named_network(host)
        if machine is not None and machine.runs_hq:
            network = "loopback"
        found[ref] = ConnectionReach(
            network=network,
            host=host,
            address=address,
            machine=entity_link("machine", machine.name) if machine else None,
            presence=machine.presence if machine else None,
        )
    return found
