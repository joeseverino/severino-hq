"""What the operator declared about each name.

The claims each declaration makes, the names that are only aliases of another,
and whether a provider fronts them. Read once per projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from hq.platform.application.routes import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.names import names_a_host, normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.control_plane.provider_spec import origin_is_authoritative

from .entity_links import EntityLink, entity_link
from .infrastructure import (
    context_for_resolution,
    enabled_resources,
    resolved_spec,
    resource_health,
)
from .projection import read_once
from .whereabouts import Origin


@dataclass(frozen=True)
class Reading:
    """One fact about a resource: what HQ asked for, and what was found.

    ``desired`` is blank where the operator authors nothing: a certificate's
    expiry is discovered, never declared. ``observed`` is blank until a
    controller has looked. They are carried together because the whole question
    a service page answers is whether they agree.
    """

    label: str
    desired: str = ""
    observed: str = ""

    @property
    def drifted(self) -> bool:
        return bool(self.desired and self.observed and self.desired != self.observed)

    @property
    def value(self) -> str:
        """The one thing to show when there is only room for one.

        What is true beats what was asked for. An operator reading a service
        page wants the world, and falls back to the declaration only where the
        world has not been looked at yet.
        """

        return self.observed or self.desired


@dataclass(frozen=True)
class Claim:
    """One resource's participation in one service, already resolved."""

    resource_key: str
    kind: str
    health: dict[str, str]
    readings: tuple[Reading, ...] = ()
    # Whether the provider answers for the name itself (its ``fronts``).
    fronted: bool = False

    @property
    def url(self) -> str:
        return entity_link("resource", self.resource_key).url

    @property
    def link(self) -> EntityLink:
        return entity_link(self.kind, self.resource_key)

    @property
    def within_service(self) -> EntityLink:
        """The link on a row about its own service: where the record leads,
        since the row is already the name it answers for."""

        link = self.link
        _name, arrow, leads_to = link.label.partition(" → ")
        return replace(link, label=leads_to) if arrow else link

    @property
    def edit_url(self) -> str:
        return reverse("control_plane:edit", kwargs={"key": self.resource_key})

    @property
    def drifted(self) -> bool:
        return any(reading.drifted for reading in self.readings)


# ----- Derivation ------------------------------------------------------------


@dataclass
class _Ledger:
    """The per-name facts one pass over the enabled declarations collects."""

    declared: dict[str, dict[str, list[Claim]]] = field(default_factory=dict)
    covering: list[tuple[str, frozenset[str], Claim]] = field(default_factory=list)
    # Origins in two ranks: a provider that also answers for a name states where
    # the name points; one that only routes states where the request is served.
    # Routed wins, resolved fills the gaps, whatever order the rows arrive in.
    routed: dict[str, str] = field(default_factory=dict)
    resolved: dict[str, str] = field(default_factory=dict)
    # Every address each name resolves to, so reachability is derived.
    answers: dict[str, list[str]] = field(default_factory=dict)
    # The certificate declarations each name's ingress serves it with.
    served_with: dict[str, set[str]] = field(default_factory=dict)

    def file(self, provider, claim: Claim, reading: tuple) -> None:
        hostnames, origin, resolves_to, certificate = reading
        if provider.covers:
            self.covering.append((provider.facet, frozenset(hostnames), claim))
            return
        for hostname in hostnames:
            self.declared.setdefault(hostname, {}).setdefault(provider.facet, []).append(
                claim
            )
            if origin:
                rank = self.routed if origin_is_authoritative(provider) else self.resolved
                rank.setdefault(hostname, origin)
            self.answers.setdefault(hostname, []).extend(resolves_to)
            if certificate:
                self.served_with.setdefault(hostname, set()).add(certificate)


def _read_declaration(provider, spec) -> tuple | None:
    """``(hostnames, origin, answers, certificate)``, or None for an unreadable spec.

    An unreadable spec is reported on its own resource's health and must not
    take every other name on the board down with it.
    """

    try:
        # Filtered once here: whether a name can be answered at is a property
        # of the name, not of the provider that published it.
        hostnames = tuple(
            name
            for name in (normalized_hostname(n) for n in provider.hostnames(spec))
            if names_a_host(name)
        )
        origin = provider.origin(spec) if provider.origin else ""
        resolves_to = provider.answers(spec) if provider.answers else ()
        certificate = provider.certificate(spec) if provider.certificate else ""
    except (KeyError, TypeError, ValueError):
        return None
    return hostnames, origin, resolves_to, certificate


def _split_aliases(declared, origins, aliases) -> dict[str, list[tuple[str, Claim]]]:
    """Move each alias's claims beside its target service.

    An alias's record belongs to the alias, not to the name it points at, so it
    is kept beside the service rather than merged into it or dropped.
    """

    alias_claims: dict[str, list[tuple[str, Claim]]] = {}
    for alias, target in aliases.items():
        for claims in declared.pop(alias, {}).values():
            for claim in claims:
                alias_claims.setdefault(target, []).append((alias, claim))
        origins.pop(alias, None)
    return alias_claims


class Declarations(NamedTuple):
    """Every enabled declaration, sorted by what it names and what it covers."""

    declared: dict[str, dict[str, list[Claim]]]
    covering: list[tuple[str, frozenset[str], Claim]]
    origins: dict[str, str]
    aliases: dict[str, str]
    alias_claims: dict[str, list[tuple[str, Claim]]]
    machines: Any
    answers: dict[str, list[str]]
    # How a page says it knows: an origin an ingress declared forwards, an
    # origin a record implied simply answers.
    routed: frozenset[str]
    served_with: dict[str, frozenset[str]]


def declarations() -> Declarations:
    """Every enabled declaration, sorted into what it names and what it covers.

    Shared by the catalogue and by a name nobody has declared anything for yet,
    so a prospective service is assembled from the same reading of the world.
    Read once per projection.
    """

    return read_once("services.declarations", _read_declarations)


def _read_declarations() -> Declarations:

    machines, targets = context_for_resolution()
    ledger = _Ledger()
    for resource in enabled_resources():
        provider = PROVIDERS.get(resource.kind)
        if provider is None or not provider.facet or provider.hostnames is None:
            continue
        reading = _read_declaration(provider, _resolved(resource, targets))
        if reading is None:
            continue
        claim = Claim(
            resource.key,
            resource.kind,
            resource_health(resource),
            _readings(provider, resource),
            fronted=_fronted(provider, resource),
        )
        ledger.file(provider, claim, reading)

    origins = {**ledger.resolved, **ledger.routed}
    aliases = _aliases(ledger.declared, origins)
    alias_claims = _split_aliases(ledger.declared, origins, aliases)
    return Declarations(
        ledger.declared,
        ledger.covering,
        origins,
        aliases,
        alias_claims,
        machines,
        ledger.answers,
        frozenset(ledger.routed),
        {name: frozenset(keys) for name, keys in ledger.served_with.items()},
    )


def _aliases(declared, origins) -> dict[str, str]:
    """``{alias: target}`` for names that are another service under a second name.

    A CNAME to a name HQ already serves is not a second service. It is the same
    service reachable another way: ``www.example.com`` pointing at
    ``example.com`` is one site, and listing it separately puts a second row on
    the board with its own health, its own certificate and its own "not routed",
    describing something that is not separate from anything.

    Only within what HQ declares. A CNAME to somewhere outside is a name HQ
    publishes and does not otherwise know about, which is a service of its own
    by every definition that matters here.
    """

    found: dict[str, str] = {}
    for hostname in declared:
        target = normalized_hostname(origins.get(hostname, ""))
        if not target or ":" in target:
            # A proxy origin, which is where a name is *served*, not another
            # name for it.
            continue
        if target != hostname and target in declared:
            found[hostname] = target
    # And the same site under the one prefix that conventionally means it.
    # A CNAME says "I am that name"; an address record says only where to go,
    # so `www.example.com` and `example.com` as two A records to one place look
    # like two services and are one site. Every other subdomain sharing an
    # address is a different service on one host (mail and a quiz sitting on
    # the same cPanel are not each other) so this is `www` and nothing else.
    for hostname in declared:
        apex = hostname.partition(".")[2]
        if not hostname.startswith("www.") or apex not in declared:
            continue
        if hostname in found or apex in found:
            continue
        here = normalized_hostname(origins.get(hostname, ""))
        there = normalized_hostname(origins.get(apex, ""))
        if here and here == there:
            found[hostname] = apex
    return found


def runtime_claim(
    origin: "Origin | None", containers: "dict[tuple[str, str], Any]"
) -> "Claim | None":
    """The declaration for the container this name is served from, if there is one.

    Matched on what the origin already resolved: a machine and a container on
    it. That is the same pair the declaration carries, so the two are the same
    thing recognised from opposite directions: one authored, one observed.
    """

    if origin is None or not origin.host or not origin.container:
        return None
    resource = containers.get((origin.host, origin.container))
    if resource is None:
        return None
    provider = PROVIDERS[resource.kind]
    return Claim(
        resource.key,
        resource.kind,
        resource_health(resource),
        _readings(provider, resource),
        fronted=_fronted(provider, resource),
    )


def _fronted(provider: Any, resource: ManagedResource) -> bool:
    """Whether the provider puts itself in front of this declaration's names."""

    if provider.fronts is None:
        return False
    try:
        return bool(provider.fronts(resource.spec))
    except (KeyError, TypeError, ValueError):
        return False


def _readings(provider: Any, resource: ManagedResource) -> tuple[Reading, ...]:
    """What this resource actually does, as its own provider describes it.

    Read from the authored spec rather than the resolved one: these are shown
    beside "what was found", and resolution is HQ's own work. Comparing a
    resolved value against an observation would report drift between two things
    the operator never wrote.
    """

    if provider.readout is None:
        return ()
    try:
        rows = provider.readout(resource.spec, resource.status or {})
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(
        Reading(label=label, desired=str(desired or ""), observed=str(observed or ""))
        for label, desired, observed in rows
        if desired or observed
    )


# Shared with the domain view, so two projections of the same declaration
# cannot disagree about which names a certificate covers.
_resolved = resolved_spec
