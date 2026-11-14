"""How a request reached HQ: the channel it arrived on, and the client address the forwarded chain proves."""

from dataclasses import dataclass

from hq.platform.core.network import client_ip, is_trusted_proxy, socket_peer, split_host_port

from .reach import NETWORKS, network_of


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

        return _TONES.get(self.id, "serious")


_TONES = {"tailnet": "good", "network": "attention", "loopback": "attention"}


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


def channel_for_request(request) -> Channel:
    """The caller channel after applying the trusted-proxy decision once."""

    channel = channel_of(client_ip(request))
    return OPAQUE_CHANNEL if _chain_is_all_proxies(request) else channel


def displayed_client_ip(request) -> str:
    """The best caller address HQ may display without authorizing from it.

    ``client_ip`` remains the sole source for admission. When an undeclared
    proxy reports a caller, the rightmost forwarded hop may still be correlated
    for explanatory UI; keeping this function explicitly presentation-only
    prevents that useful knowledge from quietly becoming network authority.
    """

    peer = socket_peer(request)
    forwarded = forwarded_chain(request)
    if forwarded and not is_trusted_proxy(peer):
        return split_host_port(forwarded[-1])[0]
    return client_ip(request)


def forwarded_chain(request) -> list[str]:
    """The X-Forwarded-For entries, in the order the hops occurred."""

    return [hop.strip() for hop in str(request.META.get("HTTP_X_FORWARDED_FOR", "")).split(",") if hop.strip()]


def _chain_is_all_proxies(request) -> bool:
    """Whether the forwarded chain identified anybody at all."""

    peer = socket_peer(request)
    forwarded = [split_host_port(hop)[0] for hop in forwarded_chain(request)]
    return bool(forwarded) and is_trusted_proxy(peer) and all(is_trusted_proxy(hop) for hop in forwarded)
