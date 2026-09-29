"""What the internet can reach, and how bad that is.

Derived from the service paths HQ already walks (``application.paths``): each
name's DNS answer, the edge in front of it, the network its answer is on, and
the machine and container that serve it. Nothing is polled for it.

A route has one of four exposures, worst first:

- **open**: the internet reaches it and nothing in front asks who is asking;
- **gated**: the internet reaches it, through a reading that admits only whom it
  allows (a reading kind that ``restricts``: an Access application, an access
  list);
- **private**: only the tailnet or the local network reaches it;
- **unrouted**: nothing HQ reads routes a name to it.

A thing's exposure is its worst route's. A problem is as urgent as the worst
exposure of what it is about: a serious advisory on an open route stays
serious, the same advisory behind a gate or on the tailnet is attention, and
one nothing routes to is information.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from control_plane.names import normalized_hostname
from control_plane.providers import PROVIDERS

from .facts import Subject, readings
from .path_model import Route
from .projection import read_once
from .reach import network_of

OPEN, GATED, PRIVATE, UNROUTED = "open", "gated", "private", "unrouted"
# Worst first: the order a page lists them and the index a ranking compares.
LEVELS = (OPEN, GATED, PRIVATE, UNROUTED)
LABELS = {
    OPEN: "Open to the internet",
    GATED: "Internet, behind a gate",
    PRIVATE: "Private networks only",
    UNROUTED: "Nothing routes to it",
}
# What a serious problem becomes at each exposure.
_SERIOUS_AT = {OPEN: "serious", GATED: "attention", PRIVATE: "attention", UNROUTED: "neutral"}
_STATUS_ORDER = ("serious", "attention", "neutral", "good")


@dataclass(frozen=True)
class RouteExposure:
    """One route to one name, and who it lets in."""

    hostname: str
    level: str
    line: str
    via: str
    # The readings that gate the name: "Behind Access: Example app".
    gates: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return LABELS[self.level]


@dataclass(frozen=True)
class Exposure:
    """The routes reaching one thing, worst first."""

    routes: tuple[RouteExposure, ...] = ()

    @property
    def level(self) -> str:
        return self.routes[0].level if self.routes else UNROUTED

    @property
    def label(self) -> str:
        return LABELS[self.level]

    @property
    def worst(self) -> RouteExposure | None:
        return self.routes[0] if self.routes else None

    @property
    def sentence(self) -> str:
        """"Open to the internet as shop.example.com", or why it is not."""

        worst = self.worst
        if worst is None:
            return LABELS[UNROUTED]
        gate = f" ({'; '.join(worst.gates)})" if worst.gates else ""
        return f"{worst.label} as {worst.hostname}{gate}"


def worse(one: str, other: str) -> str:
    return one if LEVELS.index(one) <= LEVELS.index(other) else other


def status_at(status: str, level: str) -> str:
    """How urgent a problem of ``status`` is at exposure ``level``.

    Only exposure lowers it: a serious problem nothing routes to is
    information, never the other way round. An attention item stays attention
    wherever it is reachable, and is information where nothing reaches it.
    """

    if status == "serious":
        return _SERIOUS_AT[level]
    if status == "attention" and level == UNROUTED:
        return "neutral"
    return status


def status_rank(status: str) -> int:
    return _STATUS_ORDER.index(status) if status in _STATUS_ORDER else len(_STATUS_ORDER)


def gates_of(hostname: str) -> tuple[str, ...]:
    """The readings that admit only whom they allow in front of ``hostname``."""

    subject = Subject.of(hostnames=(hostname,))
    return tuple(
        dict.fromkeys(
            f"{joined.relation}: {joined.title}".rstrip(": ")
            for joined in readings().about(subject)
            if joined.spec.restricts and joined.hostnames
        )
    )


def _public_name(route: Route) -> bool:
    """Whether the route starts at a record anyone on the internet can resolve."""

    first = route.hops[0] if route.hops else None
    kind = first.source.kind if first is not None and first.source is not None else ""
    provider = PROVIDERS.get(kind)
    return bool(provider is not None and provider.public_effect)


def route_level(hostname: str, route: Route) -> str:
    """Open, gated or private: which networks this route admits a request from."""

    networks = {network_of(hop.detail) for hop in route.hops if hop.step == "network"}
    through_edge = any(hop.step == "edge" for hop in route.hops)
    if "public" in networks or (through_edge and _public_name(route)):
        reachable = True
    elif networks:
        reachable = False
    else:
        # A public name whose answer HQ could not place: the internet can
        # resolve it, so it is not called private on a guess.
        reachable = _public_name(route)
    if not reachable:
        return PRIVATE
    return GATED if gates_of(hostname) else OPEN


def exposure_of_name(hostname: str) -> Exposure:
    """Every route to ``hostname``, worst first."""

    name = normalized_hostname(hostname)
    return read_once(f"exposure.name:{name}", lambda: _exposure_of_name(name))


def _exposure_of_name(name: str) -> Exposure:
    from .paths import path_to

    found = []
    for route in path_to(name).routes:
        level = route_level(name, route)
        found.append(
            RouteExposure(
                hostname=name,
                level=level,
                line=route.line,
                via=route.via,
                gates=gates_of(name) if level == GATED else (),
            )
        )
    return Exposure(tuple(sorted(found, key=lambda item: LEVELS.index(item.level))))


def exposure_of_names(hostnames: Iterable[str]) -> Exposure:
    """The worst-first routes over several names: what serves them all."""

    routes = [route for name in sorted(set(hostnames)) for route in exposure_of_name(name).routes]
    return Exposure(tuple(sorted(routes, key=lambda item: (LEVELS.index(item.level), item.hostname))))


def publicly_answering(addresses: Iterable[str]) -> frozenset[int]:
    """The ports a perimeter check saw answer from the internet on these addresses."""

    from control_plane.observations.host import PERIMETER_KIND

    subject = Subject.of(addresses=tuple(address for address in addresses if address))
    return frozenset(
        int(port)
        for joined in readings().about(subject, kinds=(PERIMETER_KIND,))
        for port in joined.record.get("answered_publicly") or ()
        if str(port).isdigit()
    )


def exposure_of_container(item) -> Exposure:
    """What reaches one running container: the names routed to it, and any
    port it publishes that the perimeter check saw answer from the internet."""

    return read_once(f"exposure.container:{item.address}", lambda: _exposure_of_container(item))


def _exposure_of_container(item) -> Exposure:
    named = exposure_of_names(item.serves)
    machine = item.machine
    answering = publicly_answering((*getattr(machine, "addresses", ()), getattr(machine, "address", "")))
    ports = sorted(port for port in item.running.ports if port in answering)
    direct = tuple(
        RouteExposure(
            hostname=f"{machine.name}:{port}",
            level=OPEN,
            line=f"Port {port} on {machine.name}",
            via="Published port",
        )
        for port in ports
    )
    return Exposure(tuple(sorted((*direct, *named.routes), key=lambda route: LEVELS.index(route.level))))
