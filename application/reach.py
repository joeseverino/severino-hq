"""Where a service can be reached from, derived rather than declared.

HQ already holds both halves of this sentence and has never said it out loud.
A DNS record answers with an address; an address belongs to a network; and a
network is either the tailnet, the house, or the internet. So "who can even
open a socket to this" is a fact about declarations HQ already reconciles, not
something anybody has to record.

It matters because the answer is invisible from every page that shows it today.
A rewrite pointing at a LAN address and one pointing at a tailnet address look
identical -- same provider, same health, same green tick -- and differ only in
who is able to reach the thing on the other side.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_network

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


def _classify(answer: str) -> Reach:
    try:
        address = ip_address(answer.strip())
    except ValueError:
        # A CNAME, or a name pointing at another name. Whoever it resolves to
        # decides, and that is not this record's statement to make.
        return UNKNOWN
    if any(address in network for network in TAILNET):
        return TAILNET_ONLY
    if any(address in network for network in PRIVATE):
        return LOCAL_NETWORK
    return PUBLIC
