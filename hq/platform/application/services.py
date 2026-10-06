"""A service: one hostname, and everything that has to be true for it to answer.

HQ's infrastructure registry is keyed by resource: a row per DNS rewrite, per
proxy host, per certificate. That is the right shape for a controller, which
reconciles one declaration at a time and has no opinion about the others. It is
the wrong shape for the question an operator actually asks, which is never "did
that rewrite apply" but "does this name work, and if not, which part is
missing".

The join is the hostname, and nothing new is stored to make it. A rewrite
already names one. A proxy host already lists the ones it answers for, and where
it forwards them. A certificate already covers a set of them, wildcards
included. A project already publishes to one. This module reads those four
things and puts them side by side.

There is no Service model and there should not be one. A service is a fact about
the declarations, so a stored copy could disagree with them, and the entire
value here is being the thing that cannot.

Two consequences:

- A service does not hang off a project. A repository is how something gets
  built, and much of what an operator runs was built by somebody else; keyed on
  a project, those would be unrepresentable. The project is an annotation
  on a service when one happens to publish there, and absent otherwise.
- A provider is never named here. Which providers supply which facet, and how to
  read hostnames out of their specs, is declared by the providers themselves in
  ``control_plane.providers``. This module knows there are facets, not what they
  are made of.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any
from urllib.parse import urlparse

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.names import certificate_covers, in_zone, normalized_hostname
from .labels import lower_first, plural
from hq.domains.control_plane.providers import PROVIDERS, registry_label, service_facets, resource_home
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS

from .containers import Running, container_watchers
from .entity_links import EntityLink, entity_link
from .facts import Joined, Readings, Subject, readings as stored_readings
from .infrastructure import enabled_resources
from .locate import host_of, split_endpoint
from .naming import name_context
from .derivations import derivation
from .derived_inputs import ESTATE_READS, estate_variant
from .projection import read_once
from .reach import UNKNOWN, Reach, reach_of
from .service_declarations import Claim, declarations, runtime_claim
from .ui import ListRow
from .whereabouts import Origin, Whereabouts, locate, machine_for, whereabouts
from .service_facets import CERTIFICATE_FACET, DNS_FACET, Facet, RUNTIME_FACET
from .published_sites import published_projects


HQ_MARK = "hq"
OBSERVED_MARK = "observed"


@dataclass(frozen=True)
class Service:
    hostname: str
    facets: tuple[Facet, ...]
    origin: Origin | None = None
    project: dict[str, str] | None = None
    faults: tuple[str, ...] = ()
    # Other names that reach this same service, folded in rather than listed
    # separately. See ``_aliases``.
    aliases: tuple[str, ...] = ()
    # Who can open a connection to this name, derived from the addresses it
    # resolves to. Not a declaration anybody makes: a consequence of one.
    reach: Reach = UNKNOWN
    # ``(alias, claim)`` for the declarations that make those other names work.
    # Beside the service, never merged into its facets: merged, two CNAMEs read
    # as two records competing for one name.
    alias_claims: tuple[tuple[str, "Claim"], ...] = ()
    # Whether this operator keeps it at the top. A preference about a person,
    # never part of what HQ asks the controller to make true.
    pinned: bool = False
    # Readings joined to the name that say what serves it, where no record
    # names an origin: a Pages project with this custom domain.
    served_by: tuple[str, ...] = ()
    # Readings joined to the name that supply no facet: an Access application.
    observed: tuple[Joined, ...] = ()
    # ``HQ_MARK`` for HQ's own name, ``OBSERVED_MARK`` for a name readings
    # name and nothing declares, "" for a declared service.
    mark: str = ""

    @property
    def is_hq(self) -> bool:
        return self.mark == HQ_MARK

    @property
    def is_observed(self) -> bool:
        return self.mark == OBSERVED_MARK

    @property
    def path(self):
        """The request path to this name, walked from the readings."""

        from .paths import path_to

        return path_to(self.hostname)

    @property
    def exposure(self):
        """Who can reach this name: the internet, through a gate, or only a
        private network, from the same routes the path walks."""

        from .exposure import exposure_of_name

        return exposure_of_name(self.hostname)

    @property
    def alias_summary(self) -> str:
        """What to call the folded-away records, in the reader's terms.

        "Records behind the other names" is a description of the data structure
        rather than of anything an operator has. There is almost always exactly
        one alias, and its name is the useful word.
        """

        if len(self.aliases) == 1:
            return f"Records for {self.aliases[0]}"
        return f"Records for {len(self.aliases)} other names"

    @property
    def url(self) -> str:
        return entity_link("service", self.hostname).url

    @property
    def provider_answers(self) -> str:
        """The DNS provider answering for this name itself, when a proxied record
        puts it in front; "" otherwise."""

        dns = next((facet for facet in self.facets if facet.id == DNS_FACET), None)
        if dns is None or not _served_by_the_provider(dns):
            return ""
        for claim in dns.claims:
            provider = PROVIDERS.get(claim.kind)
            for name in provider.connection_providers if provider else ():
                if name in CONNECTION_LABELS:
                    return CONNECTION_LABELS[name]
        return ""

    @property
    def project_link(self) -> EntityLink | None:
        if not self.project:
            return None
        return entity_link("project", self.project["slug"], label=self.project["name"])

    @property
    def claims(self) -> tuple[Claim, ...]:
        return tuple(claim for facet in self.facets for claim in facet.claims)

    @property
    def container(self) -> "Running | None":
        """The one container this service was found running in, if any.

        Held on the service rather than dug out of a facet by the template,
        because the page acts on it in a different place from where it reports
        it: the controls belong beside the page's other actions, not inside a
        card that is describing something.
        """

        return next(
            (facet.observed for facet in self.facets if facet.observed), None
        )

    @property
    def zone_key(self) -> str:
        """The domain HQ manages that this name lives in, if it manages one.

        A service and a domain are different pages about overlapping things,
        one is a hostname and everything that has to be true for it to answer,
        the other is a zone and every record published in it. example.com is
        both, so each page links to the other.

        Matched through the provider that says it contains records, so the tie
        is the one the registry already declares rather than a second opinion
        about what a domain is.
        """

        for resource in ManagedResource.objects.filter(enabled=True):
            provider = PROVIDERS.get(resource.kind)
            if provider is None or not provider.contains:
                continue
            zone = normalized_hostname(resource.spec.get("zone"))
            if in_zone(self.hostname, zone):
                return zone
        return ""

    @property
    def origin_is_news(self) -> bool:
        """Whether saying where this is served adds anything to the cards.

        It usually does not. Once a facet names the container and another prints
        the address it forwards to, a sentence repeating both is a third copy of
        one fact.

        It earns its place twice: when something outside answers the name, which
        no facet can report, and when the address belongs to no machine HQ
        knows, which is the one thing here worth interrupting for.
        """

        if self.origin is None:
            return False
        if self.origin.external or not self.origin.known:
            return True
        # Nothing identified what is running, so the note carries the caveat.
        return not any(facet.observed for facet in self.facets)

    @property
    def declared_claims(self) -> tuple[Claim, ...]:
        """Claims that name this service, rather than merely answering for it.

        A wildcard certificate covers a name without anyone having declared it,
        so this is what separates "somebody built this" from "something happens
        to reach it".
        """

        return tuple(
            claim
            for claim in self.claims
            if not (PROVIDERS.get(claim.kind) and PROVIDERS[claim.kind].covers)
        )

    @cached_property
    def health(self):
        """The one health rule's answer for this name (``service_health``)."""

        from .service_health import service_health

        return service_health(self)

    @cached_property
    def base_health(self):
        """The same rule without HQ's own findings: what the topology, and so
        the findings, are derived from."""

        from .service_health import service_health

        return service_health(self, own_findings=False)

    @property
    def status(self) -> str:
        """``good``, ``attention``, ``serious``, or ``unknown`` for no health reading."""

        return self.health.state

    @property
    def tone(self) -> str:
        return self.health.tone

    @property
    def status_label(self) -> str:
        return self.health.label

    @property
    def fault_rows(self) -> tuple[ListRow, ...]:
        """The faults as the host's own list rows.

        Projected here rather than marked up in a template, so a second surface
        that wants to show them renders the same thing without restating it,
        and so the badge, which is what stops the state being carried by colour
        alone, cannot be forgotten by one of them.
        """

        return tuple(
            ListRow(title=fault, status="attention", badge="Setup")
            for fault in self.faults
        )


def _certificates_in_use() -> dict[str, dict[str, Any]]:
    """The certificate each proxied name is actually served with.

    Observed, never declared. HQ does not hold the material for an internally
    signed certificate: the CA that signs it is deliberately air-gapped,
    so it can never own one, and a page that only counts what HQ declares
    would report "no certificate covers this" for names served over TLS.

    Read from the sweeps of the kinds that declare ``served_certificate``,
    because the proxy is the thing that chooses which certificate answers.
    """

    readers = {
        kind: spec.served_certificate
        for kind, spec in PROVIDERS.items()
        if spec.served_certificate is not None
    }
    found: dict[str, dict[str, Any]] = {}
    for snapshot in ProviderInventory.objects.filter(kind__in=tuple(readers)):
        for record in snapshot.records:
            served = readers[snapshot.kind](record) if isinstance(record, dict) else None
            if served is None or served.unread:
                continue
            for hostname in served.hostnames:
                found[hostname] = dict(served.certificate)
    return found


@derivation("estate.services", reads=ESTATE_READS, vary=estate_variant)
def _service_catalog() -> tuple[Service, ...]:
    """Every hostname HQ declares, assembled from the resources that name it."""

    (
        declared, covering, origins, aliases, alias_claims, machines, answers, routed,
        served_with,
    ) = declarations()
    estate = _Estate.read(covering, machines)
    by_target: dict[str, list[str]] = {}
    for alias, target in sorted(aliases.items()):
        by_target.setdefault(target, []).append(alias)
    return tuple(
        _assemble(
            hostname,
            facets,
            estate,
            origins.get(hostname, ""),
            tuple(by_target.get(hostname, ())),
            tuple(alias_claims.get(hostname, ())),
            tuple(answers.get(hostname, ())),
            hostname in routed,
            served_with.get(hostname, frozenset()),
        )
        for hostname, facets in sorted(declared.items())
    )


def ordered_services(
    found: tuple[Service, ...], favorites: tuple[str, ...]
) -> tuple[Service, ...]:
    """The operator's favorites first, in their order, then the rest by name."""

    if not favorites:
        return found
    rank = {name: index for index, name in enumerate(favorites)}
    return tuple(
        sorted(
            (
                replace(service, pinned=service.hostname.lower() in rank)
                for service in found
            ),
            key=lambda service: (
                rank.get(service.hostname.lower(), len(rank)),
                service.hostname,
            ),
        )
    )


def service_catalog(favorites: tuple[str, ...] = ()) -> tuple[Service, ...]:
    """Every derived service, the operator's favorites first.

    ``favorites`` is the operator's own order for the handful they keep at the
    top. Applied here rather than in a template so every surface that lists
    services agrees about what comes first, and so the ordering never becomes
    a property of a Service: it is a fact about a person, not a hostname.
    """

    return read_once(
        f"services.catalog:{'|'.join(favorites)}",
        lambda: ordered_services(_service_catalog(), favorites),
    )


def home_url(resource: ManagedResource) -> str:
    """The machine, service or domain page a declaration belongs to, else its own page."""

    provider = PROVIDERS.get(resource.kind)
    if provider is not None and provider.home is not None:
        return resource_home(resource)
    service = _services_by_resource().get(resource.key)
    return service.url if service is not None else resource_home(resource)


def _services_by_resource() -> dict[str, Service]:
    def load() -> dict[str, Service]:
        found: dict[str, Service] = {}
        for service in service_catalog():
            for facet in service.facets:
                for claim in facet.claims:
                    found.setdefault(claim.resource_key, service)
        return found

    return read_once("services.by_resource", load)


def find_service(hostname: str) -> Service | None:
    wanted = normalized_hostname(hostname)
    return next(
        (service for service in service_catalog() if service.hostname == wanted), None
    )


def alias_target(hostname: str) -> str:
    """The service this name is merely another name for, or "".

    A CNAME to a name HQ already serves is not a service of its own, so its
    claim is held by the name it points at, and asking about the alias answers
    with that service.
    """

    wanted = normalized_hostname(hostname)
    aliases = declarations().aliases
    return aliases.get(wanted, "")


def _serves(hostname: str, names, claim: "Claim", served_with: frozenset[str]) -> bool:
    """Whether a certificate that covers the name is the one it is served with.

    Where the ingress names the certificate it serves, only that one applies;
    covering the name is not serving it.
    """

    return certificate_covers(hostname, names) and (
        not served_with or claim.resource_key in served_with
    )


def certificates_serving(hostname: str) -> tuple[str, ...]:
    """The declared certificates a name is served with, by key. Read once per projection."""

    wanted = normalized_hostname(hostname)
    found = declarations()
    covering, served_with = found.covering, found.served_with
    named = served_with.get(wanted, frozenset())
    return tuple(
        dict.fromkeys(
            claim.resource_key
            for facet_id, names, claim in covering
            if facet_id == CERTIFICATE_FACET and _serves(wanted, names, claim, named)
        )
    )


def service_or_prospect(hostname: str) -> Service:
    """The service for this name, or the empty shape of one not declared yet.

    A service with nothing behind it is a coherent thing to look at. Every facet
    reads "not declared" and offers what could supply it, seeded with the name,
    which is exactly the page an operator wants before they have built anything.
    Nothing is stored to make one: ask for a name and this describes it, whether
    or not anything answers for it yet.
    """

    return prospects((normalized_hostname(hostname),))[0]


def prospects(hostnames: tuple[str, ...]) -> tuple[Service, ...]:
    """``service_or_prospect`` for several names, from one reading of the declarations."""

    (
        declared, covering, origins, aliases, alias_claims, machines, answers, routed,
        served_with,
    ) = declarations()
    estate = _Estate.read(covering, machines) if hostnames else None
    return tuple(
        _assemble(
            name,
            declared.get(name, {}),
            estate,
            origins.get(name, ""),
            tuple(alias for alias, target in sorted(aliases.items()) if target == name),
            tuple(alias_claims.get(name, ())),
            answers=tuple(answers.get(name, ())),
            routed=name in routed,
            served_with=served_with.get(name, frozenset()),
        )
        for name in hostnames
    )


def service_reading() -> dict[str, int]:
    """How many services there are, and how many are not fully wired."""

    catalog = service_catalog()
    return {
        "total": len(catalog),
        "incomplete": sum(1 for service in catalog if service.faults),
    }


def _container_declarations() -> dict[tuple[str, str], Any]:
    """Container declarations, keyed by the machine and name they identify."""

    return {
        (resource.spec.get("host", ""), resource.spec.get("name", "")): resource
        for resource in enabled_resources()
        if resource.kind == CONTAINER_KIND
    }


class _ContainersRunning:
    """The container sweep, read at most once and only if a service runs in one.

    A catalogue asks once per service; each asking the database made the page
    cost a query, and a parse of every container, per hostname.
    """

    def __init__(self) -> None:
        self._found: dict[tuple[str, str], tuple[dict[str, Any], Any]] | None = None

    def on(self, host: str, name: str) -> tuple[dict[str, Any], Any] | None:
        if self._found is None:
            self._found = {}
            for snapshot in ProviderInventory.objects.filter(kind=CONTAINER_KIND):
                for record in snapshot.records:
                    key = (record.get("host"), record.get("name"))
                    if all(isinstance(part, str) for part in key):
                        # The first record wins, as a scan of the sweep would find it.
                        self._found.setdefault(key, (record, snapshot.observed_at))
        return self._found.get((host, name))


@dataclass(frozen=True)
class _Estate:
    """One reading of the world, shared by every service assembled from it.

    Everything here is the same for every service in one build. What varies per
    name stays a parameter.
    """

    covering: list[tuple[str, frozenset[str], Claim]]
    projects: dict[str, dict[str, str]]
    machines: tuple[dict[str, Any], ...]
    containers: "dict[tuple[str, str], Any]"
    # What places an address: whose machine it is, and what answers there. Read
    # at most once for the whole catalogue, and not at all by a page that lists
    # no service.
    at: "Whereabouts | None" = None
    in_use: "_CertificatesInUse | None" = None
    readings: "Readings | None" = None
    running: _ContainersRunning = field(default_factory=_ContainersRunning)

    @classmethod
    def read(cls, covering, machines) -> "_Estate":
        """The readings a catalogue needs, taken once."""

        return cls(
            readings=stored_readings(),
            covering=covering,
            projects=published_projects(),
            machines=machines,
            containers=_container_declarations(),
            at=whereabouts(machines),
            # Only if something asks. Most services carry a declared
            # certificate and never reach the question; a dashboard listing
            # published sites never asks it at all, and a query nobody needs is
            # one every page pays for.
            in_use=_CertificatesInUse(_certificates_in_use),
        )


def _assemble(
    hostname: str,
    declared: dict[str, list[Claim]],
    estate: "_Estate",
    origin_address: str,
    aliases: tuple[str, ...] = (),
    alias_claims: tuple[tuple[str, "Claim"], ...] = (),
    answers: tuple[str, ...] = (),
    routed: bool = True,
    served_with: frozenset[str] = frozenset(),
) -> Service:
    covering = estate.covering
    projects = estate.projects
    machines = estate.machines
    containers = estate.containers
    in_use = estate.in_use
    by_name = (
        estate.readings.about(Subject.of(hostnames=(hostname,)))
        if estate.readings is not None
        else ()
    )
    origin = (
        replace(
            locate(origin_address, machines, estate.at),
            routed=routed,
            serving=_serving(estate.readings, origin_address),
        )
        if origin_address
        else None
    )
    context = name_context(hostname)
    # A container declaration names a machine and a container, not a hostname,
    # so nothing else ties it to the name it serves. The origin resolves both
    # halves, which is the tie.
    runtime = runtime_claim(origin, containers)
    facets = tuple(
        Facet(
            id=facet_id,
            label=label,
            claims=tuple(declared.get(facet_id, ()))
            + ((runtime,) if runtime and facet_id == RUNTIME_FACET else ())
            + tuple(
                claim
                for covered_facet, names, claim in covering
                if covered_facet == facet_id and _serves(hostname, names, claim, served_with)
            ),
            observed=_observed(facet_id, origin, estate.running),
            machine=(
                machine_for(origin, machines) if facet_id == RUNTIME_FACET else None
            ),
            context=context,
            readings=tuple(item for item in by_name if item.facet == facet_id),
        )
        for facet_id, label in service_facets()
    )
    return Service(
        observed=tuple(item for item in by_name if not item.facet),
        served_by=tuple(
            dict.fromkeys(
                item.title for item in by_name if item.facet in _SERVES and item.title
            )
        ),
        hostname=hostname,
        facets=facets,
        aliases=aliases,
        alias_claims=alias_claims,
        origin=origin,
        project=projects.get(hostname),
        faults=_faults(facets, origin, in_use, hostname),
        reach=reach_of(answers),
    )


# Reading facets that name what serves a name or an address.
_SERVES = ("runtime", "network")


def _serving(index: "Readings | None", origin_address: str) -> tuple[str, ...]:
    """Readings joined to an origin that say what serves it, by name. The
    column is "Runs on", so the name is the answer: "Example Registrar", not "on the
    network of Example Registrar". The relation is for the relationships table."""

    if index is None:
        return ()
    host = host_of(origin_address)
    found = index.about(Subject.of(hostnames=(host,), addresses=(host,)), facets=_SERVES)
    return tuple(
        dict.fromkeys(
            item.title
            for item in found
            if item.title
        )
    )


# The provider whose inventory records are containers. Named once, here, because
# the runtime card is the one surface that has to know which sweep to read; every
# other reference to it in this module goes through this.


def _observed(
    facet_id: str, origin: Origin | None, running: "_ContainersRunning"
) -> "Running | None":
    """What HQ found supplying this facet without having been told.

    Only the runtime facet can answer, because the origin has already done the
    work: a proxy forwards to an address and a port, and the
    container inventory says which container on that machine is listening.

    Never a Claim. HQ does not manage this, cannot reconcile it, and a card that
    blurred the two would offer an Edit link that edits nothing.
    """

    if facet_id != "runtime" or origin is None or not origin.container:
        return None
    found = running.on(origin.host, origin.container)
    if found is None:
        return None
    record, observed_at = found
    return Running.of(record, observed_at, container_watchers())


class _CertificatesInUse:
    """The proxy's certificates, read at most once and only if asked.

    Named for what it holds rather than for how it defers, so no call site
    reads as a plain dict lookup.
    """

    def __init__(self, read):
        self._read = read
        self._found: dict[str, dict[str, Any]] | None = None

    def covering(self, hostname: str) -> dict[str, Any] | None:
        if self._found is None:
            self._found = self._read()
        return self._found.get(hostname)


def _faults(
    facets: tuple[Facet, ...],
    origin: Origin | None,
    in_use: "_CertificatesInUse | None" = None,
    hostname: str = "",
) -> tuple[str, ...]:
    """Wiring gaps: the failures that exist only in the join.

    Deliberately not a health report. Whether a declared resource reconciled is
    already reported per resource, and repeating it here would put one problem
    in the operator's queue twice under two names. Everything below is invisible
    to any single resource, because it is a statement about how two of them
    relate.
    """

    by_id = {facet.id: facet for facet in facets}
    faults: list[str] = []

    parked = _points_nowhere(origin, by_id.get(DNS_FACET))
    if parked:
        faults.append(parked)

    for facet in facets:
        kinds = [claim.kind for claim in facet.claims]
        # Two providers of *different* kinds on one facet is normal: an
        # internal answer and a public one are both DNS and legitimately differ.
        # Two of the same kind is a contradiction: only one can win, and which
        # one is decided by whichever reconciled last.
        for kind in sorted({kind for kind in kinds if kinds.count(kind) > 1}):
            faults.append(
                f"Two {plural(lower_first(registry_label(kind)))} are set up for this name. Remove one."
            )

    # These two rules are statements about particular facets, so they name them.
    # A facet no provider supplies is not assembled, so a rule about it simply
    # does not apply, which is the right answer for a question HQ cannot ask
    # rather than a fault to report.
    proxy = by_id.get("proxy")
    certificate = by_id.get("certificate")
    if proxy is None or certificate is None:
        return tuple(faults)

    serves = proxy.present
    served_with = (
        in_use.covering(hostname)
        if serves and not certificate.present and in_use
        else None
    )
    if serves and not certificate.present and not served_with:
        faults.append(
            "Served without TLS. No certificate in HQ covers this name, "
            "and the proxy host uses none."
        )
    if serves and origin is not None and not origin.known:
        faults.append(
            f"The proxy forwards to {origin.address}, which matches no known machine."
        )
    return tuple(faults)


def _points_nowhere(origin: Origin | None, dns: "Facet | None" = None) -> str:
    """Why an address answers nothing, when that is knowable from the address.

    A parked name is a legitimate thing to have and an easy thing to forget, so
    HQ says which it is looking at rather than reporting the record as healthy
    because the record exists.

    Unless the provider answers on its own behalf. A proxied record puts the
    provider in front of the name: the address in it is a placeholder that no
    packet is meant to reach, and the redirect or page served there is the
    point. Reading that as "resolves to somewhere nothing answers" would report
    a working redirect as a fault.
    """

    if origin is None:
        return ""
    if _served_by_the_provider(dns):
        return ""
    from .reach import is_documentation

    address = split_endpoint(origin.address)[0] or origin.address
    if is_documentation(address):
        return (
            f"Resolves to {address}, a documentation address. Nothing answers there."
        )
    if address in {"0.0.0.0", "::"}:
        return f"Resolves to {address}, which is not a reachable address."
    return ""


def _served_by_the_provider(dns: "Facet | None") -> bool:
    """Whether a DNS provider answers for this name rather than forwarding it.

    A proxied record means the provider terminates the connection and does
    whatever it was told (redirect, cache, serve a page) so the address in
    the record is a placeholder no packet is meant to reach.

    Read off the claims, which carry their provider's ``fronts`` answer.
    """

    return dns is not None and any(claim.fronted for claim in dns.claims)


def service_url_for(public_url: str) -> str:
    """The service page for a published URL, when HQ manages that name.

    The reverse of the tie the service page makes, kept beside it so the two
    cannot come to disagree about which names HQ knows.
    """

    hostname = normalized_hostname(urlparse(public_url or "").hostname or "")
    if not hostname or not find_service(hostname):
        return ""
    return entity_link("service", hostname).url
