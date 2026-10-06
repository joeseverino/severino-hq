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

A thing with no route is **unknown** rather than unrouted until HQ has read every
kind that could route to it: a gap in what HQ knows is not evidence that the
internet cannot reach something, so it never lowers how urgent a problem is.

A thing's exposure is its worst route's. A problem is as urgent as the worst
exposure of what it is about: a serious advisory on an open route stays
serious, the same advisory behind a gate or on the tailnet is attention, and
one nothing routes to is information.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS

from .facts import Subject, readings
from .path_model import Route
from .projection import read_once
from .reach import network_of

OPEN, GATED, PRIVATE, UNKNOWN, UNROUTED = "open", "gated", "private", "unknown", "unrouted"
# Worst first: the order a page lists them and the index a ranking compares.
# Unknown ranks next to open, because for all HQ knows it is open.
LEVELS = (OPEN, UNKNOWN, GATED, PRIVATE, UNROUTED)
LABELS = {
    OPEN: "Open to the internet",
    GATED: "Open to the internet, behind a login",
    PRIVATE: "Tailnet or home network only",
    UNKNOWN: "Not known who can reach it",
    UNROUTED: "Not reachable",
}
# One word each, for a table column; the sentence says the rest.
SHORT = {OPEN: "Internet", GATED: "Behind a login", PRIVATE: "Private", UNKNOWN: "Unknown", UNROUTED: "Not reachable"}
# What a serious problem becomes at each exposure. Unknown keeps it serious.
_SERIOUS_AT = {
    OPEN: "serious", GATED: "attention", PRIVATE: "attention", UNKNOWN: "serious", UNROUTED: "neutral",
}
# The ports a machine's front door answers on: a request routed to the machine
# lands on whichever container publishes one of them.
FRONT_DOOR_PORTS = frozenset({80, 443})
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
    # Gates in front of some paths only, which leave the name as it was.
    path_gates: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return LABELS[self.level]


@dataclass(frozen=True)
class Exposure:
    """The routes reaching one thing, worst first."""

    routes: tuple[RouteExposure, ...] = ()
    # False when HQ has not read everything that could route to it, so an
    # empty ``routes`` means "not known" rather than "nothing".
    known: bool = True

    @property
    def level(self) -> str:
        if self.routes:
            return self.routes[0].level
        return UNROUTED if self.known else UNKNOWN

    @property
    def label(self) -> str:
        return LABELS[self.level]

    @property
    def short(self) -> str:
        return SHORT[self.level]

    @property
    def worst(self) -> RouteExposure | None:
        return self.routes[0] if self.routes else None

    @property
    def sentence(self) -> str:
        """"Open to the internet as shop.example.com", or why it is not."""

        worst = self.worst
        if worst is None:
            return LABELS[self.level]
        gate = f" ({'; '.join(worst.gates)})" if worst.gates else ""
        partial = f"; login only at {', '.join(worst.path_gates)}" if worst.path_gates else ""
        return f"{worst.label} as {worst.hostname}{gate}{partial}"


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
    """The readings that admit only whom they allow in front of all of ``hostname``."""

    return tuple(dict.fromkeys(label for label, paths in _gates(hostname) if not paths))


def path_gates_of(hostname: str) -> tuple[str, ...]:
    """The paths of ``hostname`` a gate stands in front of, when none covers
    all of it: the name stays as open as it was, and these are the parts that
    are not."""

    return tuple(dict.fromkeys(path for _label, paths in _gates(hostname) for path in paths))


def _gates(hostname: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    subject = Subject.of(hostnames=(hostname,))
    return tuple(
        (f"{joined.relation}: {joined.title}".rstrip(": "), _gated_paths(joined.record, hostname))
        for joined in readings().about(subject)
        if joined.spec.restricts and joined.hostnames
    )


def _gated_paths(record, hostname: str) -> tuple[str, ...]:
    """The paths a gate covers on ``hostname``, or () when it covers all of it.

    A gate scoped as ``host/wp-admin*`` protects that path, not the name: the
    rest of the site answers anyone. Only a bare host, ``host/`` or ``host/*``
    stands in front of everything. A gate that states no scope (an access
    list on a proxy host) covers the host it is attached to.
    """

    scopes = [str(item) for item in (record.get("domain"), *(record.get("domains") or ())) if item]
    paths = []
    for scope in scopes:
        host, _, path = scope.partition("/")
        if normalized_hostname(host) != hostname:
            continue
        if path in ("", "*"):
            return ()
        paths.append(f"/{path}")
    return tuple(paths)


def public_name(route: Route) -> bool:
    """Whether the route starts at a record anyone on the internet can resolve."""

    first = route.hops[0] if route.hops else None
    kind = first.source.kind if first is not None and first.source is not None else ""
    provider = PROVIDERS.get(kind)
    return bool(provider is not None and provider.public_effect)


def route_level(hostname: str, route: Route) -> str:
    """Open, gated or private: which networks this route admits a request from."""

    networks = {network_of(hop.detail) for hop in route.hops if hop.step == "network"}
    through_edge = any(hop.step == "edge" for hop in route.hops)
    if "public" in networks or (through_edge and public_name(route)):
        reachable = True
    elif networks:
        reachable = False
    else:
        # A public name whose answer HQ could not place: the internet can
        # resolve it, so it is not called private on a guess.
        reachable = public_name(route)
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
                path_gates=path_gates_of(name) if level == OPEN else (),
            )
        )
    return Exposure(tuple(sorted(found, key=lambda item: LEVELS.index(item.level))))


def exposure_of_names(hostnames: Iterable[str]) -> Exposure:
    """The worst-first routes over several names: what serves them all."""

    routes = [route for name in sorted(set(hostnames)) for route in exposure_of_name(name).routes]
    return Exposure(tuple(sorted(routes, key=lambda item: (LEVELS.index(item.level), item.hostname))))


def publicly_answering(addresses: Iterable[str]) -> frozenset[int]:
    """The ports a perimeter check saw answer from the internet on these addresses."""

    from hq.domains.control_plane.observations.host import PERIMETER_KIND

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


def routed_containers() -> dict[tuple[str, str], frozenset[str]]:
    """``{(machine, container): names}``: every container a walked route ends at.

    From the paths themselves, so a route HQ only reads (a proxy host nobody
    declared) counts as much as one it declares.
    """

    return read_once("exposure.routed_containers", _routed_containers)


def _routed_containers() -> dict[tuple[str, str], frozenset[str]]:
    from .paths import path_to, routed_names

    found: dict[tuple[str, str], set[str]] = {}
    for name in routed_names():
        for route in path_to(name).routes:
            machine = ""
            for hop in route.hops:
                if hop.step == "machine":
                    machine = hop.name
                elif hop.step == "container" and machine:
                    found.setdefault((machine, hop.name), set()).add(name)
                elif hop.step == "ingress" and machine:
                    # Through the machine's front door: "" stands for whichever
                    # container publishes it, which the route does not name.
                    found.setdefault((machine, ""), set()).add(name)
    return {place: frozenset(names) for place, names in found.items()}


def listening_ports(item) -> tuple[int, ...]:
    """The host ports a container answers on: what it publishes, or, on the
    host's own network, what its image exposes, since there it binds them
    directly."""

    if item.running.network_mode != "host":
        return item.running.ports
    return tuple(int(port) for port in (item.runtime or {}).get("exposed_ports") or ())


def front_door_names(item) -> frozenset[str]:
    """The names routed through this container's machine's ingress, when this
    container is the one publishing the front door: a proxy is as exposed as
    the worst route it takes in, though no route names it."""

    if not FRONT_DOOR_PORTS.intersection(listening_ports(item)):
        return frozenset()
    return routed_containers().get((item.machine.name, ""), frozenset())


def _exposure_of_container(item) -> Exposure:
    from .paths import reads_every_route

    names = {
        *item.serves,
        *routed_containers().get((item.machine.name, item.running.name), ()),
        *front_door_names(item),
    }
    named = exposure_of_names(names)
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
    # Nothing routes to it only when HQ read every routing kind and, for a
    # container publishing ports, checked which answer from the internet.
    checked = not item.running.ports or bool(_perimeter_read(machine))
    return Exposure(
        tuple(sorted((*direct, *named.routes), key=lambda route: LEVELS.index(route.level))),
        known=reads_every_route() and checked,
    )


def _perimeter_read(machine) -> bool:
    from hq.domains.control_plane.observations.host import PERIMETER_KIND

    addresses = (*getattr(machine, "addresses", ()), getattr(machine, "address", ""))
    subject = Subject.of(addresses=tuple(address for address in addresses if address))
    return bool(readings().about(subject, kinds=(PERIMETER_KIND,)))
