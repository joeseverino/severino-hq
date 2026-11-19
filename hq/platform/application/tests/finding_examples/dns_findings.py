from hq.platform.core.testing import declare

from . import Example, Examples, Raised, connection

DNS = "adguard.dns"
QUERIES = "adguard.query_summary"
ADGUARD = "example-adguard"


def adguard(ref: str = ADGUARD) -> None:
    connection(ref, "adguard")


def two_resolvers() -> None:
    adguard("example-adguard-a")
    adguard("example-adguard-b")


def served() -> None:
    """A resolver, and a name it rewrites to a service HQ declares."""

    adguard()
    declare("idle-rewrite", "adguard.rewrite", {"domain": "idle.example.com", "answer": "100.64.0.10"})


def dns(ref: str = ADGUARD, **said) -> dict:
    return {
        "connection_ref": ref,
        "protection_enabled": True,
        "filtering_enabled": True,
        "upstreams": [{"host": "dns.example.test", "transport": "tls"}, {"host": "10.0.0.53", "transport": "dns"}],
        **said,
    }


def plain(*hosts: str) -> list[dict]:
    return [{"host": host, "transport": "dns"} for host in hosts]


def looked_up(domain: str, queries: int, window_hours: float = 24.0) -> dict:
    return {
        "connection_ref": ADGUARD,
        "domain": domain,
        "queries": queries,
        "client_count": 1 if queries else 0,
        "window_hours": window_hours,
    }


def resolver(*records: dict, queries: tuple[dict, ...] = (), raises: tuple[Raised | str, ...] = ()) -> Example:
    return Example(readings={DNS: records, QUERIES: queries}, given=adguard, raises=raises)


HEALTHY = resolver(dns(), queries=(looked_up("app.example.com", 3),))

EXAMPLES = (
    Examples(
        "dns-protection-off",
        fires=(resolver(dns(protection_enabled=False), raises=("AdGuard protection is off on example-adguard",)),),
        quiet=(HEALTHY, resolver(dns(filtering_enabled=False))),
    ),
    Examples(
        "dns-filtering-off",
        fires=(resolver(dns(filtering_enabled=False), raises=("AdGuard filtering is off on example-adguard",)),),
        quiet=(
            HEALTHY,
            # Protection off is said as protection off, not as each thing it stops.
            resolver(dns(protection_enabled=False)),
        ),
    ),
    Examples(
        "dns-plain-upstream",
        fires=(
            resolver(
                dns(upstreams=plain("198.51.100.53")),
                raises=("AdGuard on example-adguard sends lookups to 198.51.100.53 unencrypted",),
            ),
            # Two servers are named, and more are counted.
            resolver(
                dns(upstreams=plain("192.0.2.1", "192.0.2.2")),
                raises=("AdGuard on example-adguard sends lookups to 192.0.2.1, 192.0.2.2 unencrypted",),
            ),
            resolver(
                dns(upstreams=plain("192.0.2.1", "192.0.2.2", "192.0.2.3")),
                raises=("AdGuard on example-adguard sends lookups to 3 servers unencrypted",),
            ),
            # A resolver with nothing unencrypted does not end the search, and each is its own, in title order.
            Example(
                readings={
                    DNS: (
                        dns("example-adguard-a"),
                        dns("example-adguard-b", upstreams=plain("192.0.2.9")),
                    )
                },
                given=two_resolvers,
                raises=("AdGuard on example-adguard-b sends lookups to 192.0.2.9 unencrypted",),
            ),
        ),
        quiet=(HEALTHY,),
    ),
    Examples(
        "dns-name-unused",
        fires=(
            # Each name nobody looked up is its own finding, on its service where HQ serves it.
            Example(
                readings={
                    DNS: (dns(),),
                    QUERIES: (
                        looked_up("app.example.com", 3),
                        looked_up("idle.example.com", 0),
                        looked_up("old.example.com", 0),
                    ),
                },
                given=served,
                raises=(
                    "No device looked up idle.example.com",
                    "No device looked up old.example.com",
                ),
            ),
        ),
        quiet=(
            HEALTHY,
            # A window shorter than half a day says too little.
            resolver(dns(), queries=(looked_up("old.example.com", 0, window_hours=6),)),
        ),
    ),
)
