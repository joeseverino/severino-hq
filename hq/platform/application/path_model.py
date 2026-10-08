"""A service path's vocabulary.

The hops a request takes, the routes that carry it, the certificates it meets,
and where each fact came from.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS

from .entity_links import EntityLink, entity_link, kind_label
from .labels import lower_first

NETWORK_LABELS = {
    "tailnet": "Tailnet",
    "network": "Local network",
    "loopback": "Loopback",
    "public": "Internet",
}


@dataclass(frozen=True, slots=True)
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
            return f"In HQ as {self.declared}, not read"
        return f"{self.label} through {self.connection}" if self.connection else self.label


@dataclass(frozen=True, slots=True)
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
        from .moments import ago

        when = self.source.observed_at if self.source else None
        return f"confirmed on this site {ago(when)}" if when else "confirmed on this site"

    @property
    def tip(self) -> str:
        """The hover card: the verdict, then which certificate, who, how long."""

        verdict = "Confirmed" if self.attestation else "Not confirmed yet"
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


@dataclass(frozen=True, slots=True)
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


def last_machine(hops: tuple[Hop, ...]) -> str:
    """The machine a walk of hops ends on, or "" when it reaches none."""

    return next((hop.name for hop in reversed(hops) if hop.step == "machine"), "")


@dataclass(frozen=True, slots=True)
class Route:
    """The hops from one DNS answer to what serves the name."""

    via: str
    hops: tuple[Hop, ...]
    # The port a TCP or UDP forward listens on, for a route that is one.
    port: int | None = None

    @property
    def via_phrase(self) -> str:
        """``via`` inside a sentence: "internal DNS record", acronyms intact."""

        return lower_first(self.via)

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
        return last_machine(self.hops)

    @property
    def unread(self) -> tuple[str, ...]:
        found = [hop.unread for hop in self.hops if hop.unread]
        found += [cert.unread for cert in self.certificates if cert.unread]
        return tuple(dict.fromkeys(found))


@dataclass(frozen=True, slots=True)
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


def dns_label(kind: str) -> str:
    """A DNS step by who can resolve it: anyone, or only the networks HQ is on."""

    provider = PROVIDERS.get(kind)
    return "External DNS" if provider is not None and provider.public_effect else "Internal DNS"


def pointing_at(link: EntityLink | None, answer: str) -> EntityLink | None:
    """A record's link worded as what it points at: the page it is on already
    names what it answers for."""

    return replace(link, label=answer) if link is not None and answer else link
