"""The request path to a hostname, hop by hop, from what the connections read.

``path_to(hostname)`` walks what a request for the name meets: the DNS answer
(a public record, proxied or not, or an internal rewrite), the ingress (the
provider edge, a proxy host, a Caddy route, a Pages project, a tunnel), then the
machine and container behind it. One route per DNS answer that names the host.

Each hop names the reading or swept record it came from and the connection
that read it. A hop HQ cannot see is stated with its reason ("not read:
<kind>, because <reason>"), never guessed. Which kinds answer which hop is read
from the registries: DNS and ingress providers declare their facet and origin,
readings their facet, ``redirects_to`` and ``upstream``. A kind nothing reads
falls back to its declarations, marked as declared.

``hq_path`` is HQ's own path: its hostname's path, ending at HQ where it says
it is served (``served_at``/``served_port``), or the machine alone where no
name leads to it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from control_plane.certificate_authorities import authority_name
from control_plane.names import is_hostname, normalized_hostname
from control_plane.observations import OBSERVATIONS
from control_plane.providers import (
    PROVIDERS,
    TAILNET_KIND,
    expiry_phrase,
)
from control_plane.connection_kinds import CONNECTION_LABELS

from .entity_links import EntityLink, entity_link, kind_label
from .facts import Joined, Subject, readings, snapshots_of
from .infrastructure import enabled_resources
from .locate import host_of, split_endpoint
from .projection import read_once
from .reach import is_documentation, network_of

# How deep an alias chain is followed before the path stops saying more.
MAX_ALIASES = 3

NETWORK_LABELS = {
    "tailnet": "Tailnet",
    "network": "Local network",
    "loopback": "Loopback",
    "public": "Internet",
}


@dataclass(frozen=True)
class Source:
    """Where a hop was read: the kind, the connection, and when; or the declaration."""

    kind: str
    connection: str = ""
    observed_at: datetime | None = None
    declared: str = ""

    @property
    def label(self) -> str:
        spec = OBSERVATIONS.get(self.kind)
        return spec.label if spec is not None else kind_label(self.kind)

    @property
    def short(self) -> str:
        """The label in a column that already names the facet: "Cloudflare Access"."""

        spec = OBSERVATIONS.get(self.kind)
        return spec.short if spec is not None else kind_label(self.kind)

    @property
    def phrase(self) -> str:
        if self.declared:
            return f"Declared as {self.declared}; not read"
        return f"{self.label} via {self.connection}" if self.connection else self.label


@dataclass(frozen=True)
class Certificate:
    """The certificate one hop presents, or why HQ cannot say."""

    role: str
    name: str = ""
    issuer: str = ""
    expiry: str = ""
    source: Source | None = None
    unread: str = ""
    # The declaration the name is served with, when HQ holds one.
    record: str = ""
    # What the controller found the name serving, when it matched what HQ
    # installed: an attestation, not a declaration.
    verified_fingerprint: str = ""
    # The name it is served for, so a browser on that name can add what it saw.
    serves: str = ""
    # "87 days left": the expiry as the one number worth reading at a glance.
    left: str = ""

    @property
    def link(self) -> EntityLink | None:
        return entity_link("resource", self.record) if self.record else None

    @property
    def attestation(self) -> str:
        """What the controller proved about the certificate, when it proved it."""

        if not self.verified_fingerprint:
            return ""
        from .ui import ago

        when = self.source.observed_at if self.source else None
        checked = f", checked {ago(when)}" if when else ""
        return (
            f"Verified: serves {self.verified_fingerprint[:12]}…, "
            f"the certificate HQ installed{checked}"
        )

    @property
    def tip(self) -> str:
        """The hover card: the verdict, then which certificate, who, how long."""

        verdict = "Verified" if self.attestation else "Not verified yet"
        facts = " · ".join(part for part in (self.name, self.issuer, self.left) if part)
        return f"{verdict}\n{facts}" if facts else verdict

    @property
    def statement(self) -> str:
        """The line, and what the controller proved about it."""

        return " · ".join(part for part in (self.line, self.attestation) if part)

    @property
    def line(self) -> str:
        if self.unread:
            return self.unread[:1].upper() + self.unread[1:]
        detail = " · ".join(part for part in (self.name, self.issuer) if part)
        text = f"{self.role} certificate {detail}".strip()
        return f"{text}, expires {self.expiry}" if self.expiry else text


# What each kind of hop looks like: a name in partials/_icon.html.
_HOP_ICONS = {
    "device": "laptop",
    "dns": "globe",
    "network": "network",
    "machine": "server",
    "origin": "server",
    "ingress": "proxy",
    "container": "container",
    "edge": "cloud",
    "served": "page",
    "external": "external",
    "hq": "home",
    "alias": "link",
    "redirect": "redirect",
    "observed": "eye",
}


@dataclass(frozen=True)
class Hop:
    """One thing a request meets on the way to what answers it."""

    step: str
    label: str
    name: str = ""
    link: EntityLink | None = None
    detail: str = ""
    source: Source | None = None
    certificate: Certificate | None = None
    unread: str = ""
    # ``(relation, link)`` for what sits over this hop: an Access application.
    overlays: tuple[tuple[str, EntityLink], ...] = ()
    # On a path walked for a request (``hq_path(request)``): what the request
    # itself carried here, whether it agrees with the readings, and the
    # admission layers decided at this hop. See ``application.request_path``.
    evidence: tuple[Any, ...] = ()
    check: Any = None
    layers: tuple[Any, ...] = ()
    # ``(text, url)`` lines under the name: what a container is running.
    facts: tuple[tuple[str, str], ...] = ()

    @property
    def icon(self) -> str:
        return _HOP_ICONS.get(self.step, "")

    @property
    def phrase(self) -> str:
        """An observed hop as a sentence fragment: its relation, and where a
        redirect sends it."""

        return f"{self.label} {self.detail}".strip() if self.detail else self.label

    @property
    def short(self) -> str:
        """The hop in the one-line path."""

        if self.step in _NAMED_BY_LABEL:
            return self.label
        if self.step in _NAMED_BY_BOTH:
            return f"{self.label} {self.name}".strip()
        return self.name or self.label


# How a step reads in the one-line path: by what it is, or by what and which.
_NAMED_BY_LABEL = frozenset({"dns", "edge", "network", "ingress"})
_NAMED_BY_BOTH = frozenset({"redirect", "served", "external"})
# Steps the one-line path leaves to the hop-by-hop view.
_DETAIL_ONLY = frozenset({"upstream"})


@dataclass(frozen=True)
class Route:
    """The hops from one DNS answer to what serves the name."""

    via: str
    hops: tuple[Hop, ...]
    # The port a TCP or UDP forward listens on, for a route that is one.
    port: int | None = None

    @property
    def via_phrase(self) -> str:
        """``via`` inside a sentence: "internal DNS record", acronyms intact."""

        # An acronym keeps its case: "HQ" stays "HQ", "Internal" becomes "internal".
        if self.via[1:2].isupper():
            return self.via
        return self.via[:1].lower() + self.via[1:]

    @property
    def line_hops(self) -> tuple[Hop, ...]:
        """The hops the one-line route shows: each machine once, no forwarding."""

        shown: list[Hop] = []
        machines: set[str] = set()
        for hop in self.hops:
            if hop.step in _DETAIL_ONLY or (hop.step == "machine" and hop.name in machines):
                continue
            if hop.step == "machine":
                machines.add(hop.name)
            shown.append(hop)
        return tuple(shown)

    @property
    def line(self) -> str:
        return " → ".join(hop.short for hop in self.line_hops)

    @property
    def certificate(self) -> Certificate | None:
        """The certificate the client sees: the first one on the path."""

        return next((hop.certificate for hop in self.hops if hop.certificate), None)

    @property
    def certificates(self) -> tuple[Certificate, ...]:
        return tuple(hop.certificate for hop in self.hops if hop.certificate)

    @property
    def redirects_to(self) -> str:
        return next((hop.name for hop in self.hops if hop.step == "redirect"), "")

    @property
    def machine(self) -> str:
        return next(
            (hop.name for hop in reversed(self.hops) if hop.step == "machine"), ""
        )

    @property
    def unread(self) -> tuple[str, ...]:
        found = [hop.unread for hop in self.hops if hop.unread]
        found += [cert.unread for cert in self.certificates if cert.unread]
        return tuple(dict.fromkeys(found))


@dataclass(frozen=True)
class ServicePath:
    hostname: str
    routes: tuple[Route, ...] = ()
    # What was read and why no route could be walked, when none could: the DNS
    # kinds read that name nothing first, then the ones not read.
    unread: tuple[str, ...] = field(default=())
    # The readings that name this host as something that answers
    # (``names_services``), each as an "observed" hop.
    observed: tuple[Hop, ...] = ()

    @property
    def observed_line(self) -> str:
        """What the observing readings say about the name, once each."""

        return " · ".join(dict.fromkeys(hop.phrase for hop in self.observed))

    @property
    def primary(self) -> Route | None:
        return self.routes[0] if self.routes else None

    @property
    def line(self) -> str:
        return self.primary.line if self.primary else ""

    @property
    def certificate(self) -> Certificate | None:
        return self.primary.certificate if self.primary else None

    @property
    def redirects_to(self) -> str:
        return next((route.redirects_to for route in self.routes if route.redirects_to), "")

    @property
    def machine(self) -> str:
        return next((route.machine for route in self.routes if route.machine), "")

    @property
    def ends_at(self) -> Hop | None:
        """The last hop of the first route: what answers, as far as HQ can see."""

        return self.primary.hops[-1] if self.primary and self.primary.hops else None

    @property
    def gaps(self) -> tuple[str, ...]:
        found = list(self.unread)
        for route in self.routes:
            found.extend(route.unread)
        return tuple(dict.fromkeys(found))


# ----- What was read ---------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    kind: str
    record: Mapping[str, Any]
    spec: Mapping[str, Any]
    source: Source
    # The declaration this record is, when HQ declares it: a read record and
    # its declaration are one thing, and the page names and links it as that.
    declaration: str = ""

    @property
    def link(self) -> EntityLink | None:
        key = self.declaration or self.source.declared
        return entity_link(self.kind, key) if key else None


def _kinds(facet: str) -> tuple[str, ...]:
    """Resource kinds that route a name for ``facet``, public ones first."""

    found = [
        kind
        for kind, provider in PROVIDERS.items()
        if provider.facet == facet
        and provider.origin is not None
        and provider.hostnames is not None
        and not provider.covers
    ]
    return tuple(sorted(found, key=lambda kind: not PROVIDERS[kind].public_effect))


def why_unread(kind: str) -> str:
    """"not read: <kind>, because <reason>", or "" when a connection read it."""

    rows = snapshots_of(kind)
    label = Source(kind).label
    if not rows:
        return f"not read: {label}, because no connection reads it"
    if any(row.reachable for row in rows):
        return ""
    reason = next((row.error for row in rows if row.error), "") or "the last read failed"
    return f"not read: {label}, because {reason.rstrip('.')}"


def _names(provider, spec) -> tuple[str, ...]:
    try:
        return tuple(normalized_hostname(str(name)) for name in provider.hostnames(spec))
    except (KeyError, TypeError, ValueError):
        return ()


def _swept(kind: str) -> dict[str, list[_Row]]:
    from .inventory import _identity

    provider = PROVIDERS[kind]
    # Matched by identity, the way the inventory decides a record is managed.
    declared = {
        _identity(kind, resource.spec): resource.key
        for resource in enabled_resources()
        if resource.kind == kind
    }
    found: dict[str, list[_Row]] = {}
    for snapshot in snapshots_of(kind):
        if not snapshot.reachable or provider.from_record is None:
            continue
        for record in snapshot.records or ():
            try:
                spec = provider.from_record(record)
            except (KeyError, TypeError, ValueError):
                continue
            source = Source(kind, str(record.get("connection_ref", "") or ""), snapshot.observed_at)
            declaration = declared.get(_identity(kind, spec), "")
            for name in _names(provider, spec):
                found.setdefault(name, []).append(_Row(kind, record, spec, source, declaration))
    return found


def _declared(kind: str) -> dict[str, list[_Row]]:
    provider = PROVIDERS[kind]
    found: dict[str, list[_Row]] = {}
    for resource in enabled_resources():
        if resource.kind != kind:
            continue
        source = Source(kind, str(resource.spec.get("connection_ref", "") or ""), declared=resource.key)
        for name in _names(provider, resource.spec):
            found.setdefault(name, []).append(_Row(kind, {}, resource.spec, source))
    return found


def _rows(kind: str) -> dict[str, list[_Row]]:
    """Records of one routing kind by the hostname each names, read once.

    Swept records when a connection read the kind; its declarations otherwise.
    """

    return read_once(
        f"paths.rows:{kind}", lambda: _declared(kind) if why_unread(kind) else _swept(kind)
    )


def _about(hostname: str, **filters: Any) -> tuple[Joined, ...]:
    return readings().about(Subject.of(hostnames=(hostname,)), **filters)


def _reading_source(joined: Joined) -> Source:
    return Source(joined.kind, joined.connection_ref, joined.observed_at)


def _reading_kinds(test) -> tuple[str, ...]:
    return tuple(kind for kind, spec in OBSERVATIONS.items() if test(spec))


def _reading_gap(kinds: Iterable[str], name: str = "") -> str:
    """Why a kind that could answer this hop was not read: the whole kind, or,
    for ``name``, a part of it refused where the name is."""

    kinds = tuple(kinds)
    whole = next((reason for kind in kinds if (reason := why_unread(kind))), "")
    if whole or not name:
        return whole
    from .facts import refusals_about

    return next(
        (refused.phrase for refused in refusals_about(Subject.of(hostnames=(name,)), kinds)), ""
    )


# ----- The walk --------------------------------------------------------------


def path_to(hostname: str) -> ServicePath:
    """Every route a request for ``hostname`` can take, from what HQ reads."""

    name = normalized_hostname(hostname)
    return read_once(f"paths.to:{name}", lambda: _path(name))


def _path(name: str) -> ServicePath:
    if not is_hostname(name):
        return ServicePath(name, (), ("Not a host name.",))
    observed = observers(name)
    routes = _to_hq(name, tuple(route for route in _routes(name, depth=0) if route.hops))
    if routes:
        return ServicePath(name, routes, observed=observed)
    return ServicePath(name, (), _no_route(), observed=observed)


def _no_route() -> tuple[str, ...]:
    """Why no route was walked: the DNS kinds read that name nothing, then each
    kind not read and why."""

    kinds = _kinds("dns")
    gaps = tuple(reason for kind in kinds if (reason := why_unread(kind)))
    read = [Source(kind).label for kind in kinds if not why_unread(kind)]
    if not read:
        return gaps
    if not gaps:
        return ("No DNS record HQ reads names this host.",)
    return (f"No {' or '.join(_lower_first(label) for label in read)} names this host.", *gaps)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def observers(name: str) -> tuple[Hop, ...]:
    """The readings that name ``name`` as something that answers, as hops."""

    found = []
    for joined in _about(name):
        if not joined.spec.names_services:
            continue
        target = joined.spec.redirects_to(joined.record) if joined.spec.redirects else ""
        found.append(
            Hop(
                "observed",
                joined.relation,
                joined.title,
                entity_link(joined.kind, "", record=joined.record),
                detail=target,
                source=_reading_source(joined),
            )
        )
    return tuple(found)


def _routes(name: str, *, depth: int) -> list[Route]:
    found = []
    for kind in _kinds("dns"):
        for row in _rows(kind).get(name, ()):
            route = Route(Source(kind).label, tuple(_from_dns(name, row, depth)))
            found.append(route)
            found.extend(_forwards(route))
    return found


def _forwards(route: Route) -> list[Route]:
    """The TCP and UDP forwards on the ingress a route reaches, one route each.

    A forward listens on the ingress's machine, so the name reaches it on its
    port. Only the ingress's own connection's forwards, read, count.
    """

    ingress = next((hop for hop in route.hops if hop.step == "ingress" and hop.source), None)
    if ingress is None:
        return []
    provider = PROVIDERS.get(ingress.source.kind)
    connections = frozenset(provider.connection_providers) if provider else frozenset()
    before = route.hops[: route.hops.index(ingress)]
    found = []
    for kind, spec in OBSERVATIONS.items():
        if not spec.forwards or spec.facet or spec.provider not in connections:
            continue
        for joined in _forward_records(kind, ingress.source.connection):
            target = spec.upstream(joined.record, "")
            if not target:
                continue
            port = int(joined.record.get("incoming_port") or 0) or None
            hop = Hop(
                "ingress",
                spec.label,
                f"port {port}" if port else "",
                detail=joined.title,
                source=_reading_source(joined),
            )
            found.append(Route(joined.title, (*before, hop, *_upstream(target)), port=port))
    return found


def _forward_records(kind: str, connection: str) -> tuple[Joined, ...]:
    """Every stored record of a forwarding kind read through ``connection``."""

    spec = OBSERVATIONS[kind]
    found = []
    for snapshot in snapshots_of(kind):
        if not snapshot.reachable:
            continue
        for record in snapshot.records or ():
            ref = str(record.get("connection_ref", "") or "")
            if connection and ref and ref != connection:
                continue
            found.append(Joined(spec, record, ref, snapshot.observed_at))
    return tuple(found)


def _answer(row: _Row) -> str:
    provider = PROVIDERS[row.kind]
    try:
        return str(provider.origin(row.spec) or "")
    except (KeyError, TypeError, ValueError):
        return ""


def _fronted(row: _Row) -> bool:
    provider = PROVIDERS[row.kind]
    if provider.fronts is None:
        return False
    try:
        return bool(provider.fronts(row.spec))
    except (KeyError, TypeError, ValueError):
        return False


def _from_dns(name: str, row: _Row, depth: int) -> list[Hop]:
    answer = _answer(row)
    fronted = _fronted(row)
    record_type = str(row.spec.get("record_type", "") or "")
    hops = [
        Hop(
            "dns",
            row.source.label,
            f"{record_type} {answer}".strip(),
            row.link,
            detail="Proxied" if fronted else "",
            source=row.source,
        )
    ]
    if fronted:
        edge, done = _edge(name, row)
        hops.extend(edge)
        if done:
            return hops
    hops.extend(_from_answer(name, answer, fronted=fronted, depth=depth))
    return hops


def _edge(name: str, row: _Row) -> tuple[list[Hop], bool]:
    """The provider answering in front of the name, and whether it ends the path."""

    overlays = tuple(
        (joined.relation, entity_link(joined.kind, "", record=joined.record))
        for joined in _about(name)
        if not joined.facet and not joined.spec.redirects and joined.spec.names_services
    )
    terminal = _redirect(name) or _served(name) or _tunnel(name)
    hop = Hop(
        "edge",
        "Edge",
        _edge_operator(row.kind),
        overlays=overlays,
        source=row.source,
        certificate=_edge_certificate(name, row.kind),
        # Whether the edge redirects the name is unknown while a connected
        # redirect reading, or a part of one, is refused.
        unread="" if terminal else _refused_gap(_redirect_kinds(), name),
    )
    if terminal:
        return [hop, *terminal], True
    return [hop], False


def _refused_gap(kinds: tuple[str, ...], name: str) -> str:
    """``_reading_gap`` for kinds a connection reads; "" where none does."""

    read = tuple(kind for kind in kinds if snapshots_of(kind))
    return _reading_gap(read, name) if read else ""


def _redirect_kinds(at_ingress: bool = False) -> tuple[str, ...]:
    """Redirect readings answered at the edge, or at a machine's ingress."""

    return _reading_kinds(lambda spec: spec.redirects and (spec.facet == "proxy") == at_ingress)


def _edge_operator(kind: str) -> str:
    """Who answers in front of the name: the fronting kind's connection provider."""

    return next(
        (
            CONNECTION_LABELS[name]
            for name in PROVIDERS[kind].connection_providers
            if name in CONNECTION_LABELS
        ),
        kind_label(kind),
    )


def _edge_certificate(name: str, fronting_kind: str) -> Certificate:
    kinds = _reading_kinds(
        lambda spec: spec.facet == "certificate" and spec.fronted_by == fronting_kind
    )
    found = _about(name, kinds=kinds)
    if found:
        return _certificate_of("Edge", found)
    gap = _reading_gap(kinds)
    return Certificate("Edge", unread=gap or "no edge certificate HQ reads covers this name")


def _certificate_of(role: str, found: tuple[Joined, ...]) -> Certificate:
    """One certificate from the readings covering a name: every issuer, the earliest expiry."""

    from .ui import moment

    dated = [(when, joined) for joined in found if (when := moment(joined.expires))]
    earliest = min(dated, key=lambda pair: pair[0])[1] if dated else found[0]
    return Certificate(
        role,
        name=found[0].title,
        issuer=", ".join(dict.fromkeys(joined.issuer for joined in found if joined.issuer)),
        expiry=earliest.expiry,
        source=_reading_source(found[0]),
    )


def _redirect(name: str, *, at_ingress: bool = False) -> list[Hop]:
    """A redirect answering for the name: at the edge, or at the machine's ingress."""

    for joined in _about(name, kinds=_redirect_kinds(at_ingress)):
        target = joined.spec.redirects_to(joined.record)
        if target:
            return [
                Hop(
                    "redirect",
                    joined.relation,
                    target,
                    entity_link("service", target),
                    detail=str(joined.record.get("status_code") or ""),
                    source=_reading_source(joined),
                    certificate=_ingress_certificate(name) if at_ingress else None,
                )
            ]
    return []


def _not_found(name: str) -> list[Hop]:
    """An ingress that answers the name with 404 on purpose."""

    for joined in _about(name, facets=("proxy",)):
        spec = joined.spec
        if spec.redirects or spec.forwards:
            continue
        return [
            Hop(
                "origin",
                "Answers 404",
                "",
                entity_link(joined.kind, "", record=joined.record),
                detail=joined.label,
                source=_reading_source(joined),
                certificate=_ingress_certificate(name),
            )
        ]
    return []


def _ingress_certificate(name: str) -> Certificate | None:
    """The certificate an ingress reading says it serves the name with."""

    kinds = _reading_kinds(lambda spec: spec.facet == "certificate" and not spec.fronted_by)
    found = _about(name, kinds=kinds)
    return _certificate_of("Served", found) if found else None


def _served(name: str) -> list[Hop]:
    """A reading that serves the name itself: a Pages project."""

    for joined in _about(name, facets=("runtime",)):
        return [
            Hop(
                "served",
                joined.label,
                joined.title,
                entity_link(joined.kind, "", record=joined.record),
                detail=joined.relation,
                source=_reading_source(joined),
            )
        ]
    return []


def _tunnel(name: str) -> list[Hop]:
    for joined in _about(name, facets=("proxy",)):
        if not joined.spec.forwards:
            continue
        upstream = joined.spec.upstream(joined.record, name)
        hops = [
            Hop(
                "ingress",
                joined.label,
                joined.title,
                entity_link(joined.kind, "", record=joined.record),
                detail=joined.relation,
                source=_reading_source(joined),
            )
        ]
        if upstream:
            hops.extend(_upstream(_endpoint(upstream), joined.addresses or _connector(joined)))
        return hops
    return []


def _connector(joined: Joined) -> tuple[str, ...]:
    return tuple(host_of(address) for address in joined.spec.addresses(joined.record))


def _endpoint(value: str) -> str:
    """``host:port`` from a URL or an endpoint."""

    text = str(value or "")
    if "://" in text:
        text = text.split("://", 1)[1].split("/", 1)[0]
    return text


def _from_answer(name: str, answer: str, *, fronted: bool, depth: int) -> list[Hop]:
    """From the address or name a record answers with, to what serves ``name``."""

    host = host_of(answer)
    network = network_of(host)
    if not network:
        return _alias(name, host, fronted=fronted, depth=depth)
    if is_documentation(host):
        return [_placeholder(name, host, fronted)]
    hops = [Hop("network", NETWORK_LABELS.get(network, network), "", detail=host)]
    machine = _machine(host)
    if machine is None:
        hops.append(
            Hop(
                "machine",
                "Machine",
                host,
                unread=f"not read: machine, because no machine HQ knows holds {host}",
            )
        )
        return hops
    hops.append(machine)
    hops.extend(_ingress(name, behind_edge=fronted, on=machine.name))
    return hops


def _placeholder(name: str, host: str, fronted: bool) -> Hop:
    if fronted:
        gap = _reading_gap(_redirect_kinds(), name)
        return Hop(
            "origin",
            "Edge answers",
            host,
            detail="A documentation address: the edge answers, nothing behind it does.",
            unread=gap,
        )
    return Hop("origin", "Parked", host, detail="A documentation address. Nothing answers there.")


def _alias(name: str, target: str, *, fronted: bool, depth: int) -> list[Hop]:
    """A record answering with another name: that name's path, or who runs it."""

    target = normalized_hostname(target)
    if not target:
        return []
    served = _served(target)
    if served:
        return served
    if depth < MAX_ALIASES and target != name:
        routes = _routes(target, depth=depth + 1)
        if routes:
            return [
                Hop("alias", "Alias of", target, entity_link("service", target)),
                *routes[0].hops,
            ]
    from .known_hosts import operator

    return [
        Hop(
            "external",
            "Served outside",
            operator(target),
            detail=target,
        )
    ]


def _machine(address: str) -> Hop | None:
    from .services import machine_link, whereabouts

    link = machine_link(address, at=_whereabouts(whereabouts))
    if link is None:
        return None
    return Hop("machine", "Machine", link.name, link.link, detail=address, source=_machine_source(link.name))


def _machine_source(name: str) -> Source | None:
    """Where HQ knows a machine from: its tailnet reading, else its declaration."""

    from .connections import machines_once

    machine = next((item for item in machines_once() if item.name == name), None)
    if machine is None:
        return None
    presence = machine.presence
    if presence is not None:
        return Source(TAILNET_KIND, presence.connection_ref, presence.observed_at)
    return Source("machine", declared=machine.declaration) if machine.declaration else None


def _container_reading(name: str, host: str) -> tuple[Source | None, tuple[tuple[str, str], ...]]:
    """The container reading that reports ``name`` on ``host``, and what it says
    is running: Docker's status, and the commit the image was built from."""

    from .services import CONTAINER_KIND

    for snapshot in snapshots_of(CONTAINER_KIND):
        for record in snapshot.records or ():
            if record.get("name") == name and record.get("host") == host:
                source = Source(CONTAINER_KIND, str(record.get("connection_ref", "") or ""), snapshot.observed_at)
                return source, _running_facts(record)
    return None, ()


def _running_facts(record: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """``(text, url)``: how long it has been up, the commit its image was built
    from (linked where the image names its repository), and whether what it
    runs is current and safe."""

    from .containers import standing_of, uptime_of

    uptime = uptime_of(str(record.get("status", "") or ""))
    revision = str(record.get("revision", "") or "")
    repository = str(record.get("source", "") or "").rstrip("/")
    facts = [(uptime, "")] if uptime else []
    if revision:
        url = f"{repository}/commit/{revision}" if repository.startswith("https://") else ""
        facts.append((revision[:7], url))
    image = str(record.get("image", "") or "")
    if image:
        # The standing says what the signature said and more: verified before
        # deploying, a newer release out, or an advisory against this version.
        facts.append((standing_of(image, str(record.get("host", "")), str(record.get("name", ""))).summary, ""))
    return tuple(facts)


def _whereabouts(build):
    return read_once("paths.whereabouts", build)


def _ingress(name: str, *, behind_edge: bool, on: str = "") -> list[Hop]:
    """The proxy that answers for ``name`` on the machine ``on``, then what it forwards to."""

    kinds = _kinds("proxy")
    for kind in kinds:
        for row in _rows(kind).get(name, ()):
            hop = Hop(
                "ingress",
                row.source.label,
                row.declaration or row.source.declared or row.source.connection or row.source.label,
                row.link,
                detail=_answer(row),
                source=row.source,
                certificate=_served_certificate(name, row, "Origin" if behind_edge else "Served"),
            )
            return [hop, *_upstream(_answer(row), on=on)]
    answered = _redirect(name, at_ingress=True) or _not_found(name)
    if answered:
        return answered
    gap = _reading_gap(kinds)
    if gap:
        return [Hop("ingress", "Ingress", unread=gap)]
    return []


def _served_certificate(name: str, row: _Row, role: str) -> Certificate:
    declared = _declared_served(name, role)
    if declared is not None:
        return declared
    provider = PROVIDERS[row.kind]
    if provider.served_certificate is not None and row.record:
        served = provider.served_certificate(row.record)
        if served is not None and served.unread:
            return Certificate(role, unread=f"not read: {role} certificate, because {served.unread}")
        if served is not None:
            certificate = served.certificate
            return Certificate(
                role,
                name=str(certificate.get("name", "") or ""),
                issuer=authority_name(certificate.get("provider")),
                expiry=expiry_phrase(str(certificate.get("expires_on", "") or "")),
                source=row.source,
                left=_days_left(str(certificate.get("expires_on", "") or "")),
            )
    reason = (
        f"{row.source.label} does not report the certificate it serves"
        if provider.served_certificate is None
        else f"{row.source.label} reports no certificate for this name"
    )
    return Certificate(role, unread=f"not read: {role} certificate, because {reason}")


def _days_left(stamp: str) -> str:
    from .expiry import days_until
    from .ui import counted, moment

    when = moment(stamp) if stamp else None
    if when is None:
        return ""
    days = days_until(when)
    return "expired" if days < 0 else f"{counted(days, 'day')} left"


def _declared_served(name: str, role: str) -> Certificate | None:
    """The certificate HQ declares for the name, as HQ knows it.

    Its issuer and expiry are HQ's own record of what it issued or uploaded,
    and the controller checks each name it installs for, so whether the name
    serves it is a reading too. A name that serves something else says so
    rather than showing the declaration as if it were in place.
    """

    from .services import certificates_serving

    keys = certificates_serving(name)
    resource = next((item for item in enabled_resources() if keys and item.key == keys[0]), None)
    if resource is None:
        return None
    status = resource.status or {}
    source = Source(resource.kind, observed_at=resource.last_observed_at)
    checked = next(
        (
            check
            for check in status.get("consumers") or ()
            if isinstance(check, dict) and normalized_hostname(check.get("domain", "")) == name
        ),
        None,
    )
    if checked is not None and checked.get("matches_expected") is False:
        return Certificate(
            role,
            source=source,
            unread=f"not verified: {name} serves a certificate other than {resource.key}",
            record=resource.key,
        )
    return Certificate(
        role,
        name=resource.key,
        issuer=str(status.get("issuer", "") or ""),
        expiry=expiry_phrase(str(status.get("not_after", "") or "")),
        source=source,
        record=resource.key,
        serves=name,
        left=_days_left(str(status.get("not_after", "") or "")),
        verified_fingerprint=(
            str(checked.get("fingerprint_sha256", "") or "")
            if checked is not None and checked.get("matches_expected") is True
            else ""
        ),
    )


def _upstream(address: str, connector: tuple[str, ...] = (), on: str = "") -> list[Hop]:
    from .services import locate, whereabouts

    if not address:
        return []
    host, _port = split_endpoint(address)
    at = _whereabouts(whereabouts)
    origin = locate(address, at=at, near=on)
    if not origin.host and network_of(host) == "loopback" and connector:
        origin = locate(connector[0], at=at)
    hops = [Hop("upstream", "Forwards to", address)]
    if origin.host:
        hops.append(
            Hop(
                "machine",
                "Machine",
                origin.host,
                entity_link("machine", origin.host),
                source=_machine_source(origin.host),
            )
        )
    if origin.container:
        from .containers import container_watchers

        source, running = _container_reading(origin.container, origin.host)
        # Its page, when a declaration watches it: every other hop names its
        # page, and the container was the one a click could not reach.
        watcher = container_watchers().get((origin.host, origin.container), ("", False))[0]
        hops.append(
            Hop(
                "container",
                "Container",
                origin.container,
                # Named as the container, not its declaration's key: the machine
                # is the hop before it, and the key said it again.
                entity_link("resource", watcher, label=origin.container) if watcher else None,
                source=source,
                facts=running,
            )
        )
    return hops


# ----- HQ itself -------------------------------------------------------------


def hq_path(request: Any = None, *, found: Any = None) -> ServicePath | None:
    """HQ's own path; for a request, from the caller's device, each hop joined
    to what the request shows (``request_path.joined``).

    ``found`` is the request's ``connection.Connection`` when the caller holds it.
    """

    from .connections import machines_once
    from .hq_self import hq_service

    own = hq_service(request, catalog=machines_once())
    if own is None:
        return None
    walked = path_to(_asked_for(request, own))
    if request is None:
        return walked
    from .request_path import joined

    return joined(walked, request, found)


def _asked_for(request: Any, own: Any) -> str:
    """The name the request asked for when it is one of HQ's, else HQ's first."""

    if request is None:
        return own.hostname
    from core.network import split_host_port

    host = normalized_hostname(split_host_port(request.get_host())[0])
    return host if host in own.hostnames else own.hostname


def _to_hq(name: str, routes: tuple[Route, ...]) -> tuple[Route, ...]:
    """Routes to one of HQ's own names, each ending at HQ; one via HQ's machine
    when no DNS answer leads there."""

    from .connections import machines_once
    from .hq_self import hq_service

    own = hq_service(catalog=machines_once())
    if own is None or name not in own.hostnames:
        return routes
    if not routes:
        machine = (
            (Hop("machine", "Machine", own.machine, entity_link("machine", own.machine)),)
            if own.machine
            else ()
        )
        routes = (Route("HQ", machine),)
    return tuple(Route(route.via, (*route.hops, _hq_hop(own))) for route in routes)


def _hq_hop(own: Any) -> Hop:
    from .hq_self import scoped_served_at, scoped_served_port

    served = ", ".join(scoped_served_at())
    port = scoped_served_port()
    return Hop(
        "hq",
        "HQ",
        own.label or own.hostname,
        entity_link("service", own.hostname),
        detail=f"Answering at {served}:{port}" if served and port else "",
    )


# ----- What a name depends on, and what depends on it -------------------------

# What changing or losing a hop does to the name, by step.
_CONSEQUENCES = {
    "dns": "The name stops resolving, or resolves somewhere else.",
    "edge": "Unproxied, the origin is reached directly and the edge certificate no longer applies.",
    "redirect": "Visitors stop being sent on.",
    "served": "The site stops answering at this name.",
    "ingress": "Requests stop reaching what it forwards to.",
    "machine": "Everything on this path through it stops answering.",
    # The address a record answers with: move it and the record still points
    # at the old one.
    "network": "The record still points at the old address, and the name stops answering.",
    "upstream": "The proxy forwards to nothing, and requests fail with a bad gateway.",
    "container": "The service stops answering.",
    "hq": "HQ stops answering at this name.",
}
_CERTIFICATE_CONSEQUENCE = "Clients see a certificate error once it expires or stops covering the name."


def consequence_of(hop: "Hop") -> str:
    """What changing this hop would do, or "" where nothing depends on it."""

    return _CONSEQUENCES.get(hop.step, "")


@dataclass(frozen=True)
class Dependency:
    """One part a name depends on, and what changing it does."""

    label: str
    name: str
    link: EntityLink | None
    consequence: str
    source: Source | None = None


@dataclass(frozen=True)
class Dependent:
    """One thing that depends on a name."""

    relation: str
    name: str
    link: EntityLink | None


def depends_on(path: ServicePath) -> tuple[Dependency, ...]:
    """The parts every route of ``path`` passes through, once each."""

    found: dict[tuple[str, str], Dependency] = {}
    # A forward on another port is a second way in, not a part the name needs.
    for route in (route for route in path.routes if route.port is None):
        for hop in route.hops:
            consequence = _CONSEQUENCES.get(hop.step)
            if consequence and hop.name:
                found.setdefault(
                    (hop.step, hop.name),
                    Dependency(hop.label, hop.name, hop.link, consequence, hop.source),
                )
            certificate = hop.certificate
            if certificate is not None and certificate.name:
                found.setdefault(
                    ("certificate", certificate.name),
                    Dependency(
                        f"{certificate.role} certificate",
                        certificate.name,
                        certificate.link,
                        _CERTIFICATE_CONSEQUENCE,
                        certificate.source,
                    ),
                )
    return tuple(found.values())


def depended_on_by(hostname: str) -> tuple[Dependent, ...]:
    """Listed services whose path leads to ``hostname``: a redirect or an alias."""

    from .service_list import listed_services

    wanted = normalized_hostname(hostname)
    found = []
    for service in listed_services():
        if service.hostname == wanted:
            found.extend(
                Dependent("Another name for it", alias, None) for alias in service.aliases
            )
            continue
        relation = _leads_to(service.path, wanted)
        if relation:
            found.append(Dependent(relation, service.hostname, entity_link("service", service.hostname)))
    return tuple(found)


def _leads_to(path: ServicePath, hostname: str) -> str:
    for route in path.routes:
        for hop in route.hops:
            if hop.name == hostname and hop.step == "redirect":
                return "Redirects here"
            if hop.name == hostname and hop.step == "alias":
                return "Resolves through it"
    return ""
