"""Where a service can be reached from, derived rather than declared.

HQ already holds both halves of this sentence and has never said it out loud.
A DNS record answers with an address; an address belongs to a network; and a
network is either the tailnet, the house, or the internet. So "who can even
open a socket to this" is a fact about declarations HQ already reconciles, not
something anybody has to record.

It matters because the answer is invisible from every page that shows it today.
A rewrite pointing at a LAN address and one pointing at a tailnet address look
identical (same provider, same health, same green tick) and differ only in
who is able to reach the thing on the other side.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv6Network, ip_address, ip_network
from pathlib import Path

# Tailscale hands out addresses from the carrier-grade NAT range and one IPv6
# ULA prefix. Nothing else on a normal network uses either, so an address in
# them is reachable by tailnet members and by nobody else.
TAILNET = (ip_network("100.64.0.0/10"), ip_network("fd7a:115c:a1e0::/48"))
PRIVATE = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
)
# Reserved for documentation. Nothing answers at one: a name pointed there is
# parked.
DOCUMENTATION = (
    ip_network("192.0.2.0/24"),
    ip_network("198.51.100.0/24"),
    ip_network("203.0.113.0/24"),
    ip_network("2001:db8::/32"),
)
# The same ranges, fixed: `DOCUMENTATION` is what fixtures narrow.
_DOCUMENTATION_RANGES = DOCUMENTATION


def is_public(address: str) -> bool:
    """Whether an address is reachable from the internet: not private, tailnet,
    loopback, link-local or documentation."""

    try:
        found = ip_address(str(address or "").strip())
    except ValueError:
        return False
    if found.is_link_local or found.is_multicast or found.is_unspecified or found.is_reserved:
        return False
    # Special-purpose ranges (192.0.0.0/24 and the like) are not the internet.
    # The documentation ranges are left to `is_documentation`, which fixtures use.
    if not found.is_global and not any(found in network for network in _DOCUMENTATION_RANGES):
        return False
    return network_of(str(found)) == "public" and not is_documentation(str(found))


def public_label(address: str) -> str:
    """How a public address is shown and counted: itself, or for IPv6 its /64,
    which privacy addresses rotate inside. "" for anything that is not an address."""

    try:
        found = ip_address(str(address or "").strip())
    except ValueError:
        return ""
    if found.version == 6:
        return str(ip_network(f"{found}/64", strict=False))
    return str(found)


def is_documentation(address: str) -> bool:
    try:
        found = ip_address(str(address or "").strip())
    except ValueError:
        return False
    return any(found in network for network in DOCUMENTATION)


@dataclass(frozen=True)
class Reach:
    """Who can open a connection to a name, and what said so."""

    id: str
    label: str
    detail: str

    @property
    def tailnet_only(self) -> bool:
        return self.id == "tailnet"


TAILNET_ONLY = Reach(
    "tailnet",
    "Tailnet only",
    "Answers with a tailnet address, so nothing off the tailnet can open a "
    "connection to it at all.",
)
LOCAL_NETWORK = Reach(
    "network",
    "Tailnet and your network",
    "Answers with a private address, so anything on that network can reach it "
    "whether or not it is on the tailnet.",
)
PUBLIC = Reach(
    "public",
    "The internet",
    "Answers with a public address.",
)
UNKNOWN = Reach("unknown", "", "")


def reach_of(answers: tuple[str, ...]) -> Reach:
    """The widest audience any of these answers admits.

    Widest, not narrowest: a name with one tailnet answer and one LAN answer is
    reachable from the LAN, and reporting the stricter of the two would describe
    a boundary that is not there.
    """

    found = [_classify(answer) for answer in answers]
    for candidate in (PUBLIC, LOCAL_NETWORK, TAILNET_ONLY):
        if candidate in found:
            return candidate
    return UNKNOWN


# Where the kernel lists this host's IPv6 addresses, one per line:
# address, interface index, prefix length, scope, flags, interface name.
IF_INET6 = Path("/proc/net/if_inet6")
# Interfaces whose prefixes say nothing about the network the host sits on.
_NOT_ON_LINK = ("lo", "tailscale", "docker", "br-", "veth")


def on_link_networks(source: Path = IF_INET6) -> tuple[IPv6Network, ...]:
    """The global IPv6 prefixes this host is directly attached to.

    A global address is public by type, yet two machines in one house share a
    prefix: an address inside one of these is on this host's own network. Read
    from the kernel on each call, since router advertisements can change them.
    Empty where the file does not exist.
    """

    try:
        lines = source.read_text().splitlines()
    except OSError:
        return ()
    found = set()
    for line in lines:
        fields = line.split()
        if len(fields) != 6 or fields[3] != "00" or fields[5].startswith(_NOT_ON_LINK):
            continue
        hexed = fields[0]
        address = ":".join(hexed[index : index + 4] for index in range(0, 32, 4))
        try:
            found.add(ip_network(f"{address}/{int(fields[2], 16)}", strict=False))
        except ValueError:
            continue
    return tuple(sorted(found))


def network_of(address: str) -> str:
    """Which network an address belongs to, as a bare identifier.

    The ranges live here once and every surface that needs to know reads them
    through this. Two implementations of "is this address on the tailnet" would
    be believed equally and disagree eventually, and the surfaces asking are a
    reachability badge and an access decision.

    Returns "" for anything that is not an address, because the callers differ
    on what that means: a DNS answer that is a name defers to whatever it
    resolves to, and a caller's address that will not parse is simply not one.
    """

    try:
        found = ip_address(str(address or "").strip())
    except ValueError:
        return ""
    if found.is_loopback:
        return "loopback"
    if any(found in network for network in TAILNET):
        return "tailnet"
    if any(found in network for network in PRIVATE):
        return "network"
    return "public"


def _classify(answer: str) -> Reach:
    return {
        "tailnet": TAILNET_ONLY,
        # A service answering on loopback is reachable from the machine it runs
        # on, which is the narrowest form of "the network it is on".
        "loopback": LOCAL_NETWORK,
        "network": LOCAL_NETWORK,
        "public": PUBLIC,
    }.get(network_of(answer), UNKNOWN)
