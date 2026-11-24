from collections.abc import Callable

from hq.platform.core.testing import declare

from ...tone import Tone
from ..service_world import declare_world, healthy
from ..test_paths import CLOUDFLARE, PUBLIC_RANGE, declare_certificate, estate, record, store
from . import Example, Examples, Raised

APP, SHOP = (
    {"host": "lab-1", "name": "app", "ports": [8000], "state": "running"},
    {"host": "edge-1", "name": "shop", "ports": [8080], "state": "running"},
)
OLD = (record("shop.example.com", "A", "198.51.100.20"), record("old.example.com", "A", "198.51.100.20", proxied=False))
DB = record("db.example.com", "A", "198.51.100.20", proxied=False)
DB_REWRITE = {"domain": "db.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"}
APP_REWRITE = {"domain": "app.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"}
SPLIT = {"cloudflare.dns_record": (OLD[0], DB), "adguard.rewrite": (APP_REWRITE, DB_REWRITE)}
CHANGE = "infrastructure.resource.update"
UNPUBLISHED = "app.example.com forwards to an unpublished port on lab-1"
UNPUBLISHED_ROUTE = {"domain_names": ["app.example.com"], "forward_host": "100.64.0.10", "forward_port": 8099}
SPLIT_TITLE = "db.example.com goes to edge-1 from the internet and lab-1 from home"


def gate(name: str, domain: str, kind: str = "self_hosted") -> dict:
    return {"connection_ref": CLOUDFLARE, "id": "a1", "name": name, "domain": domain, "type": kind}


def open_on(*ports: int) -> dict:
    """The edge's public address, answering the internet on ``ports``."""

    return {
        "record": "perimeter",
        "connection_ref": "example-ssh",
        "public_addresses": ["198.51.100.20"],
        "ports_checked": list(ports),
        "answered_publicly": list(ports),
    }


def on_estate(read: dict, *declared: tuple[str, str, dict], raises: tuple[Raised | str, ...] = ()) -> Example:
    """``test_paths.estate``, with ``read`` stored over its readings and ``declared`` held as (key, kind, spec)."""

    def given():
        estate()
        for kind, records in read.items():
            store(kind, *records)
        for key, kind, spec in declared:
            declare(key, kind, spec)
        return PUBLIC_RANGE

    return Example(given=given, raises=raises)


def declared(*rows: Callable[[], object], raises: tuple[str, ...] = ()) -> Example:
    return Example(given=lambda: [row() for row in rows], raises=raises)


def rewrite(key: str, domain: str, answer: str) -> Callable[[], object]:
    """An internal answer HQ declares, read healthy."""

    return lambda: healthy(declare(key, "adguard.rewrite", {"domain": domain, "answer": answer}))


CONSISTENT = on_estate({})

EXAMPLES = (
    Examples(
        "served-certificate-not-held",
        fires=(
            Example(
                given=lambda: declare_certificate(matches=False),
                raises=(
                    Raised(
                        title="app.example.com serves a certificate other than example-wildcard",
                        presses=("infrastructure.resource.reconcile",),
                    ),
                ),
            ),
        ),
        quiet=(Example(given=lambda: declare_certificate(matches=True)),),
    ),
    Examples(
        "public-name-served-by-nothing",
        fires=(
            on_estate(
                {"cloudflare.dns_record": OLD},
                raises=("old.example.com leads nowhere",),
            ),
        ),
        quiet=(
            CONSISTENT,
            # A machine HQ reads no containers on is unknown, not unserved.
            on_estate({"cloudflare.dns_record": OLD, "portainer.container": (APP,)}),
            # A proxy on its machine's own network serves the names that reach the machine.
            on_estate(
                {
                    "cloudflare.dns_record": OLD,
                    "portainer.container": (
                        APP,
                        {"host": "edge-1", "name": "proxy", "ports": [], "network_mode": "host", "state": "running"},
                    ),
                    "portainer.runtime": (
                        {
                            "host": "edge-1",
                            "container": "proxy",
                            "network_mode": "host",
                            "exposed_ports": [80, 443],
                            "connection_ref": "example-portainer",
                        },
                    ),
                }
            ),
        ),
    ),
    Examples(
        "route-to-stopped-container",
        fires=(
            on_estate(
                {"portainer.container": (APP, {**SHOP, "state": "exited"})},
                raises=("shop.example.com leads to shop on edge-1, which is stopped",),
            ),
            on_estate(
                {"npm.proxy_host": ({**UNPUBLISHED_ROUTE, "forward_scheme": "http", "connection_ref": "example-npm"},)},
                raises=(Raised(title=UNPUBLISHED, severity=Tone.ATTENTION),),
            ),
        ),
        quiet=(CONSISTENT,),
    ),
    Examples(
        "gate-guards-nothing",
        fires=(
            on_estate(
                {"cloudflare.access_app": (gate("Old admin", "gone.example.com"),)},
                raises=("gone.example.com has an access rule but no DNS record",),
            ),
        ),
        # One on a domain HQ does not read names nothing it could see.
        quiet=(
            CONSISTENT,
            on_estate({"cloudflare.access_app": (gate("Login", "team.provider.example/warp", "warp"),)}),
        ),
    ),
    Examples(
        "split-horizon-disagrees",
        fires=(
            on_estate(SPLIT, raises=(SPLIT_TITLE,)),
            # Each record HQ declares is offered its change.
            on_estate(
                SPLIT,
                ("example-db-rewrite", "adguard.rewrite", DB_REWRITE),
                raises=(Raised(title=SPLIT_TITLE, presses=(CHANGE,)),),
            ),
            on_estate(
                SPLIT,
                ("example-db-rewrite", "adguard.rewrite", DB_REWRITE),
                ("example-db-record", "cloudflare.dns_record", DB),
                raises=(
                    Raised(
                        title=SPLIT_TITLE,
                        presses=(CHANGE, CHANGE),
                    ),
                ),
            ),
        ),
        quiet=(
            CONSISTENT,
            # A proxy that carries both answers to one machine agrees.
            on_estate({"cloudflare.dns_record": (OLD[0], {**OLD[1], "name": "app.example.com"})}),
            # So does an edge that proxies on to the internal machine.
            on_estate(
                {
                    "caddy.route": (
                        {
                            "connection_ref": "example-edge",
                            "domain": "shop.example.com",
                            "upstream": "100.64.0.10:8000",
                        },
                    ),
                    "adguard.rewrite": (APP_REWRITE, {**APP_REWRITE, "domain": "shop.example.com"}),
                }
            ),
        ),
    ),
    Examples(
        "published-port-unfronted",
        fires=(
            # shop answers too, but a route leads to it.
            on_estate(
                {
                    "portainer.container": (APP, SHOP, {**SHOP, "name": "stray", "ports": [9000]}),
                    "host.perimeter": (open_on(8080, 9000),),
                },
                raises=(Raised(title="stray on edge-1 is open to the internet on port 9000", severity=Tone.SERIOUS),),
            ),
        ),
        # The proxy every route enters through is what is in front.
        quiet=(
            CONSISTENT,
            on_estate(
                {
                    "portainer.container": (APP, SHOP, {**SHOP, "name": "caddy", "ports": [80, 443]}),
                    "host.perimeter": (open_on(80, 443),),
                }
            ),
        ),
    ),
    Examples(
        "service-wiring-incomplete",
        # Two declarations of one kind contradict each other.
        fires=(
            declared(
                lambda: declare_world(containers=True),
                rewrite("dns-a", "app.example.com", "10.0.0.10"),
                rewrite("dns-b", "app.example.com", "10.0.0.10"),
                raises=("app.example.com: Two internal DNS records are set up for this name",),
            ),
        ),
        # An internal rewrite per address family does not compete.
        quiet=(
            declared(
                lambda: declare_world(containers=True),
                rewrite("dns-a", "app.example.com", "10.0.0.10"),
                rewrite("dns-b", "app.example.com", "fd00::10"),
            ),
        ),
    ),
)
