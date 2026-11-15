"""How a request reached HQ: the channel it arrived on, and the client address the forwarded chain proves."""

from dataclasses import dataclass

from hq.platform.core.network import PROXY, client_ip, forwarded_hops, split_host_port

from .reach import NETWORKS, network_of
from .tone import Tone


@dataclass(frozen=True, slots=True)
class Channel:
    """Which network the caller is on, decided by arithmetic alone.

    Cheap on purpose: this is the part the header badge needs on every page,
    and a badge that costs a query is a badge on every page that costs a query.
    """

    id: str
    label: str
    detail: str

    @property
    def private(self) -> bool:
        return self.id in {"tailnet", "network", "loopback"}

    @property
    def tone(self) -> str:
        """The tone of the network a caller is on: the tailnet the private way
        in, a private network the one to notice, any other the exposed one."""

        return _TONES.get(self.id, Tone.SERIOUS)


_TONES = {"tailnet": Tone.GOOD, "network": Tone.ATTENTION, "loopback": Tone.ATTENTION}


TAILNET_CHANNEL = Channel(
    "tailnet",
    NETWORKS["tailnet"],
    "This request came from a tailnet address.",
)
NETWORK_CHANNEL = Channel(
    "network",
    NETWORKS["network"],
    "This request came from a private address on the network HQ is on.",
)
LOOPBACK_CHANNEL = Channel("loopback", NETWORKS["loopback"], "This request never left the machine HQ runs on.")
OPAQUE_CHANNEL = Channel(
    "opaque",
    "Address not passed through",
    "Every hop is a trusted proxy, so HQ has only the proxy's address.",
)
ELSEWHERE_CHANNEL = Channel(
    "elsewhere",
    "Unrecognized",
    "This address is in none of the ranges HQ accepts.",
)
# What HQ does with the chain a peer outside its trusted proxies forwarded.
UNTRUSTED_FORWARDER = "Not a trusted proxy, so HQ ignores the address it forwarded."


def encryption(channel: Channel, secure: bool) -> str:
    """The encryption a request on ``channel`` can prove, ``secure`` when it arrived over TLS."""

    if channel.id == "tailnet":
        return "WireGuard + TLS" if secure else "WireGuard only"
    return "TLS" if secure else "Encryption not confirmed"


def channel_of(address: str) -> Channel:
    """What to call the network an address is on. No database, no query.

    The ranges themselves are `reach`'s: it already decides this for the DNS
    answers a service resolves to, and an address is on the tailnet or it is
    not regardless of which surface is asking. What belongs here is only the
    wording, because these sentences are about a caller rather than a service.
    """

    return {
        "tailnet": TAILNET_CHANNEL,
        "loopback": LOOPBACK_CHANNEL,
        "network": NETWORK_CHANNEL,
        "public": ELSEWHERE_CHANNEL,
    }.get(network_of(address), ELSEWHERE_CHANNEL)


@dataclass(frozen=True, slots=True)
class Forwarding:
    """Who handed a request to HQ, each hop with the part HQ gave it.

    The one trusted-proxy decision of a request's reading: everything that
    asks whether a proxy forwarded it, whether HQ trusts that proxy or which
    address it names reads this value, so no two of them can disagree.
    """

    # The address HQ judges and records, as ForwardedCaller resolved it.
    client: str = ""
    # ``(address, role)`` for each address that handled the request, in the
    # order the hops occurred, the socket peer last.
    hops: tuple[tuple[str, str], ...] = ()

    @property
    def peer(self) -> str:
        """The address that opened the socket to HQ."""

        return self.hops[-1][0] if self.hops else ""

    @property
    def forwarded(self) -> bool:
        return len(self.hops) > 1

    @property
    def trusted(self) -> bool:
        """A proxy forwarded the request, and HQ trusts it."""

        return self.forwarded and self.hops[-1][1] == PROXY

    @property
    def untrusted(self) -> bool:
        return self.forwarded and not self.trusted

    @property
    def opaque(self) -> bool:
        """Every hop is a trusted proxy, so the chain never named the caller."""

        return self.forwarded and all(role == PROXY for _, role in self.hops)

    @property
    def proxies(self) -> int:
        return sum(role == PROXY for _, role in self.hops)

    @property
    def reported(self) -> str:
        """The best caller address HQ may display without authorizing from it.

        ``client`` remains the sole source for admission. When an undeclared
        proxy reports a caller, the rightmost forwarded hop may still be
        correlated for explanatory UI; this one is presentation only, so that
        knowledge never quietly becomes network authority.
        """

        return split_host_port(self.hops[-2][0])[0] if self.untrusted else self.client

    @property
    def channel(self) -> Channel:
        """The caller's channel; a chain that never named the caller is its own answer."""

        return OPAQUE_CHANNEL if self.opaque else channel_of(self.client)


def forwarding_of(request) -> Forwarding:
    """How ``request`` was forwarded to HQ."""

    return Forwarding(client_ip(request), forwarded_hops(request))


def channel_for_request(request) -> Channel:
    """The caller's channel, for a page that reads nothing else of the request."""

    return forwarding_of(request).channel
