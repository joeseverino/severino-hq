"""A service: one hostname, and everything that has to be true for it to answer.

HQ's infrastructure registry is keyed by resource -- a row per DNS rewrite, per
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
the declarations, so a stored copy could disagree with them -- and the entire
value here is being the thing that cannot.

Two consequences worth stating, because both were the wrong way round in an
earlier sketch of this:

- A service does not hang off a project. A repository is how something gets
  built, and much of what an operator runs was built by somebody else; keyed on
  a project, those would have been unrepresentable. The project is an annotation
  on a service when one happens to publish there, and absent otherwise.
- A provider is never named here. Which providers supply which facet, and how to
  read hostnames out of their specs, is declared by the providers themselves in
  ``control_plane.providers``. This module knows there are facets, not what they
  are made of.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from django.urls import reverse

from control_plane.models import (
    ManagedResource,
    ProviderInventory,
    TopologySnapshot,
)
from control_plane.providers import (
    PROVIDERS,
    certificate_covers,
    service_facets,
    names_a_host,
    normalized_hostname,
)
from projects.models import Project

from .infrastructure import NotFoundError, resolved_spec, resource_health
from .ui import ListRow


# One spelling of a name, shared with every other surface that joins on one.
# See ``control_plane.providers.normalized_hostname``: this module had its own
# copy, the domain view had another, and they agreed by coincidence.
_normalise = normalized_hostname


def _lower_first(text: str) -> str:
    """A label as it reads mid-sentence, leaving an acronym alone.

    "Proxy host" belongs lowercase after "Add"; "TLS certificate" does not, and
    lowering its first letter produced "tLS certificate". Only the first word is
    inspected, because that is the only one being changed.
    """

    first = text.partition(" ")[0]
    if not text or first.isupper():
        return text
    return text[:1].lower() + text[1:]


@dataclass(frozen=True)
class Reading:
    """One fact about a resource: what HQ asked for, and what was found.

    ``desired`` is blank where the operator authors nothing -- a certificate's
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

    @property
    def url(self) -> str:
        return reverse("control_plane:detail", kwargs={"key": self.resource_key})

    @property
    def edit_url(self) -> str:
        return reverse("control_plane:edit", kwargs={"key": self.resource_key})

    @property
    def drifted(self) -> bool:
        return any(reading.drifted for reading in self.readings)


@dataclass(frozen=True)
class Facet:
    """One thing that has to be true for a hostname to answer, and whether it is."""

    id: str
    label: str
    claims: tuple[Claim, ...] = ()
    # What HQ can see supplying this that no declaration accounts for. A facet
    # has three states, not two, and collapsing the middle one is what made a
    # fully working service read "Not declared -- add a container stack" beside
    # a card naming the container it was already running in. "Nothing supplies
    # this" was false, and the offer it led with was to build a second one.
    observed: "Running | None" = None

    @property
    def present(self) -> bool:
        return bool(self.claims)

    @property
    def declarable(self) -> tuple[tuple[str, str], ...]:
        """``(kind, label)`` for each provider that could supply this facet.

        Read from the registry rather than listed here, so the offer to add one
        appears for a provider declared long after this was written. Only kinds
        that can be seeded from a hostname.

        A certificate is offered too, which it was not: the exclusion was
        written when every certificate predated HQ owning them, and choosing
        from what exists was the only sensible act. It stops being sensible the
        first time a domain arrives that no wildcard covers -- and this offer is
        only ever rendered for a facet nothing supplies, so a name already
        covered is never invited to grow a certificate of its own.

        The label comes with it because the page offered "Add
        cloudflare.dns_record" -- the identifier, which names the provider
        correctly and the offer not at all. Every provider already says what it
        is called in a sentence.
        """

        return tuple(
            sorted(
                # Only the first letter is lowered. Lowercasing the whole
                # label turned "Internal DNS record" into "internal dns
                # record" and shouted at nobody about the acronym.
                (kind, _lower_first(provider.label or kind))
                for kind, provider in PROVIDERS.items()
                if provider.facet == self.id and provider.seed is not None
            )
        )

    @property
    def routes(self) -> bool:
        """Whether providers of this facet exist to say where a name is served.

        Read from the registry: a provider that declares an ``origin`` hook is
        one whose job includes answering "and then what serves it". Used to tell
        a facet that is genuinely missing from one that cannot apply, because a
        name resolving straight to something outside is already routed and needs
        nothing on this network to answer for it.
        """

        return any(
            provider.origin is not None
            for provider in PROVIDERS.values()
            if provider.facet == self.id
        )

    @property
    def state(self) -> str:
        """``good``, ``attention`` or ``serious`` -- blank when nothing supplies it.

        Blank rather than a state, because an absence is not a health reading.
        Colouring "no certificate declared" as a failure would claim HQ had
        looked at something and found it wrong, when in fact there is nothing to
        look at, and the two call for different reactions.
        """

        if not self.claims:
            return ""
        states = {claim.health["state"] for claim in self.claims}
        if "degraded" in states:
            return "serious"
        return "attention" if states - {"healthy"} else "good"


@dataclass(frozen=True)
class Running:
    """A container a controller last saw, described as a person would read it.

    Built from the sweep rather than from a declaration, so every field here is
    something that was true at ``observed_at`` and may not be now. The page says
    when, because a container list with no timestamp invites being read as live.
    """

    name: str
    host: str
    stack: str
    image: str
    state: str
    status: str
    ports: tuple[int, ...]
    connection_ref: str
    observed_at: Any

    @classmethod
    def of(cls, record: dict[str, Any], observed_at: Any) -> "Running":
        return cls(
            name=str(record.get("name", "")),
            host=str(record.get("host", "")),
            stack=str(record.get("stack", "")),
            image=str(record.get("image", "")),
            state=str(record.get("state", "")),
            status=str(record.get("status", "")),
            ports=tuple(
                int(port) for port in record.get("ports") or () if str(port).isdigit()
            ),
            connection_ref=str(record.get("connection_ref", "")),
            observed_at=observed_at,
        )

    @property
    def healthy(self) -> bool:
        return self.state == "running"

    @property
    def published(self) -> str:
        return ", ".join(str(port) for port in self.ports)

    @property
    def image_label(self) -> str:
        """The image, short enough to read in a card.

        A digest-pinned image is a seventy-character line whose last twelve
        characters are the only part that distinguishes two of them, and printed
        whole it pushed every other fact on the card out of view. The repository
        and the head of the digest is what an operator compares.
        """

        repository, marker, digest = self.image.partition("@")
        if not marker:
            return self.image
        _, _, hexadecimal = digest.partition(":")
        return f"{repository}@{hexadecimal[:12]}"


@dataclass(frozen=True)
class Origin:
    """Where a request for this hostname is finally served."""

    address: str
    host: str = ""
    container: str = ""

    @property
    def external(self) -> bool:
        """Whether this is served somewhere HQ does not reach.

        A proxy forwards to ``host:port`` by construction; a DNS record names a
        target with no port. So an address with no port came from the record
        itself, which means the name is answered outside this network -- a
        Pages site, a mail host, someone else's server.

        Worth separating from "unknown host", which is the same missing lookup
        with a very different meaning: an ingress pointing at an address no host
        claims is a thing HQ cannot describe and probably should.
        """

        return not self.known and ":" not in self.address

    @property
    def operator(self) -> str:
        """What a person calls whoever serves this, read off the name itself."""

        from .known_hosts import operator

        return operator(self.address) if self.external else ""

    @property
    def known(self) -> bool:
        """Whether the address belongs to something in the topology.

        An ingress forwarding to an address no host claims is not necessarily
        broken -- but it is somewhere HQ cannot describe, reconcile or reach, and
        that is worth saying out loud rather than printing a bare IP.
        """

        return bool(self.host)

    @property
    def label(self) -> str:
        if self.container:
            return f"{self.host} · {self.container}"
        return self.host or self.address


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
    # ``(alias, claim)`` for the declarations that make those other names work.
    # Beside the service, never merged into its facets: merged, two CNAMEs read
    # as two records competing for one name.
    alias_claims: tuple[tuple[str, "Claim"], ...] = ()

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
        return reverse("control_plane:service", kwargs={"hostname": self.hostname})

    @property
    def claims(self) -> tuple[Claim, ...]:
        return tuple(claim for facet in self.facets for claim in facet.claims)

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

    @property
    def status(self) -> str:
        """Worst news first, with a live failure outranking a wiring gap.

        A degraded resource is something that was working and is not. A fault is
        a name that was never fully wired. Both need attention and only one is
        an outage, so they are not the same colour.
        """

        if not self.declared_claims:
            # Nothing declares this name, which is not health. Reported as
            # "Wired", it was the most confident statement on a page about a
            # service that did not exist.
            #
            # Covering claims do not count. A wildcard certificate answers for
            # a name without anyone having declared it, so a hostname nobody
            # has built anything for still arrives here holding one -- and
            # "this name has TLS" is true and is not the same as "this name
            # works".
            return "unknown"
        states = {facet.state for facet in self.facets}
        if "serious" in states:
            return "serious"
        return "attention" if self.faults or "attention" in states else "good"

    @property
    def status_label(self) -> str:
        """Why the state is what it is, not just how bad it is.

        Two quite different things share ``attention`` and must not share a
        word. A name with a gap in its wiring is missing something an operator
        has to go and declare. A name whose parts are all declared but which no
        controller has observed yet is complete and merely unproven -- it is
        what every service looks like for the first few minutes of its life, and
        calling that "Incomplete" sends someone looking for a hole that is not
        there.
        """

        if self.status == "serious":
            return "Degraded"
        if self.status == "unknown":
            return "Nothing declared"
        if self.status == "good":
            return "Wired"
        return "Incomplete" if self.faults else "Unverified"

    @property
    def fault_rows(self) -> tuple[ListRow, ...]:
        """The faults as the host's own list rows.

        Projected here rather than marked up in a template, so a second surface
        that wants to show them renders the same thing without restating it --
        and so the badge, which is what stops the state being carried by colour
        alone, cannot be forgotten by one of them.
        """

        return tuple(
            ListRow(title=fault, status="attention", badge="Wiring")
            for fault in self.faults
        )


# ----- Derivation ------------------------------------------------------------


def _declarations():
    """Every enabled declaration, sorted into what it names and what it covers.

    Shared by the catalogue and by a name nobody has declared anything for yet,
    so a prospective service is assembled from exactly the same reading of the
    world. Built only for the catalogue, a prospect was handed an empty covering
    list and reported "no certificate" for a name a wildcard already covered --
    a page that existed to say what was still needed, understating what was
    already there.
    """

    topology = _topology()
    declared: dict[str, dict[str, list[Claim]]] = {}
    covering: list[tuple[str, frozenset[str], Claim]] = []
    origins: dict[str, str] = {}

    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or not provider.facet or provider.hostnames is None:
            continue
        spec = _resolved(resource, topology)
        try:
            # Filtered once here rather than per provider, because "is this a
            # name something can answer at" is a property of the name and not
            # of whichever provider published it.
            hostnames = tuple(
                name
                for name in (_normalise(n) for n in provider.hostnames(spec))
                if names_a_host(name)
            )
            origin = provider.origin(spec) if provider.origin else ""
        except (KeyError, TypeError, ValueError):
            # A spec HQ cannot read is a problem with that resource, and the
            # resource's own health is where it is reported. It must not take
            # every other name on the board down with it.
            continue
        claim = Claim(
            resource.key,
            resource.kind,
            resource_health(resource),
            _readings(provider, resource),
        )
        if provider.covers:
            covering.append((provider.facet, frozenset(hostnames), claim))
            continue
        for hostname in hostnames:
            declared.setdefault(hostname, {}).setdefault(provider.facet, []).append(
                claim
            )
            if origin:
                origins.setdefault(hostname, origin)

    aliases = _aliases(declared, origins)
    alias_claims: dict[str, list[tuple[str, Claim]]] = {}
    for alias, target in aliases.items():
        # Kept beside the service rather than merged into it. Merged, the two
        # CNAMEs looked like two records fighting over one name -- HQ raised
        # "only one of them can be the answer" and called a working site
        # incomplete. They are not competing: one is another name for the other,
        # and its record belongs to the alias, not to the name it points at.
        #
        # Dropped entirely, which is what happened first, the CNAME that makes
        # www work appeared on no service page at all: a real resource, still
        # reconciled, invisible everywhere it mattered.
        for claims in declared.pop(alias, {}).values():
            for claim in claims:
                alias_claims.setdefault(target, []).append((alias, claim))
        origins.pop(alias, None)
    return declared, covering, origins, aliases, alias_claims, topology


def _aliases(declared, origins) -> dict[str, str]:
    """``{alias: target}`` for names that are another service under a second name.

    A CNAME to a name HQ already serves is not a second service. It is the same
    service reachable another way -- ``www.example.com`` pointing at
    ``example.com`` is one site, and listing it separately puts a second row on
    the board with its own health, its own certificate and its own "not routed",
    describing something that is not separate from anything.

    Only within what HQ declares. A CNAME to somewhere outside is a name HQ
    publishes and does not otherwise know about, which is a service of its own
    by every definition that matters here.
    """

    found: dict[str, str] = {}
    for hostname in declared:
        target = _normalise(origins.get(hostname, ""))
        if not target or ":" in target:
            # A proxy origin, which is where a name is *served*, not another
            # name for it.
            continue
        if target != hostname and target in declared:
            found[hostname] = target
    return found


def service_catalog() -> tuple[Service, ...]:
    """Every hostname HQ declares, assembled from the resources that name it."""

    declared, covering, origins, aliases, alias_claims, topology = _declarations()
    projects = _published_projects()
    by_target: dict[str, list[str]] = {}
    for alias, target in sorted(aliases.items()):
        by_target.setdefault(target, []).append(alias)
    return tuple(
        _assemble(
            hostname, facets, covering, origins.get(hostname, ""), projects,
            topology, tuple(by_target.get(hostname, ())),
            tuple(alias_claims.get(hostname, ())),
        )
        for hostname, facets in sorted(declared.items())
    )


def find_service(hostname: str) -> Service | None:
    wanted = _normalise(hostname)
    return next(
        (service for service in service_catalog() if service.hostname == wanted), None
    )


def alias_target(hostname: str) -> str:
    """The service this name is merely another name for, or "".

    A CNAME to a name HQ already serves is not a service of its own, so its
    claim is held by the name it points at. Asked about the alias directly,
    the page that resulted had no claim to show and said nothing was declared
    -- about a name whose record was listed as healthy one screen away. The
    honest answer is not an empty page but the service it is an alias of.
    """

    wanted = _normalise(hostname)
    _, _, _, aliases, _, _ = _declarations()
    return aliases.get(wanted, "")


def service_or_prospect(hostname: str) -> Service:
    """The service for this name, or the empty shape of one not declared yet.

    Publishing something meant creating a resource before there was anywhere to
    stand: the picker asked which kind of thing to add, then the form asked for
    the hostname, and only after saving did a page exist that knew what else the
    name still needed -- so the second resource meant typing the name again.

    A service with nothing behind it is a coherent thing to look at. Every facet
    reads "not declared" and offers what could supply it, seeded with the name,
    which is exactly the page an operator wants before they have built anything.
    Nothing is stored to make one: ask for a name and this describes it, whether
    or not anything answers for it yet.
    """

    wanted = _normalise(hostname)
    declared, covering, origins, aliases, alias_claims, topology = _declarations()
    return _assemble(
        wanted,
        declared.get(wanted, {}),
        covering,
        origins.get(wanted, ""),
        _published_projects(),
        topology,
        tuple(alias for alias, target in sorted(aliases.items()) if target == wanted),
        tuple(alias_claims.get(wanted, ())),
    )


def service_reading() -> dict[str, int]:
    """How many services there are, and how many are not fully wired."""

    catalog = service_catalog()
    return {
        "total": len(catalog),
        "incomplete": sum(1 for service in catalog if service.faults),
    }


def _assemble(
    hostname: str,
    declared: dict[str, list[Claim]],
    covering: list[tuple[str, frozenset[str], Claim]],
    origin_address: str,
    projects: dict[str, dict[str, str]],
    topology: dict[str, Any] | None,
    aliases: tuple[str, ...] = (),
    alias_claims: tuple[tuple[str, "Claim"], ...] = (),
) -> Service:
    origin = _locate(origin_address, topology) if origin_address else None
    facets = tuple(
        Facet(
            id=facet_id,
            label=label,
            claims=tuple(declared.get(facet_id, ()))
            + tuple(
                claim
                for covered_facet, names, claim in covering
                if covered_facet == facet_id
                and certificate_covers(hostname, names)
            ),
            observed=_observed(facet_id, origin),
        )
        for facet_id, label in service_facets()
    )
    return Service(
        hostname=hostname,
        facets=facets,
        aliases=aliases,
        alias_claims=alias_claims,
        origin=origin,
        project=projects.get(hostname),
        faults=_faults(facets, origin),
    )


def _observed(facet_id: str, origin: Origin | None) -> "Running | None":
    """What HQ found supplying this facet without having been told.

    Only the runtime facet can answer today, and only because the origin has
    already done the work: a proxy forwards to an address and a port, and the
    container inventory says which container on that machine is listening. Both
    facts existed and nothing joined them, so the page asked to declare
    something it could already name.

    Never a Claim. HQ does not manage this, cannot reconcile it, and a card that
    blurred the two would offer an Edit link that edits nothing.
    """

    if facet_id != "runtime" or origin is None or not origin.container:
        return None
    for snapshot in ProviderInventory.objects.filter(kind="portainer.stack"):
        for record in snapshot.records:
            if (
                record.get("host") == origin.host
                and record.get("name") == origin.container
            ):
                return Running.of(record, snapshot.observed_at)
    return None


def _faults(facets: tuple[Facet, ...], origin: Origin | None) -> tuple[str, ...]:
    """Wiring gaps -- the failures that exist only in the join.

    Deliberately not a health report. Whether a declared resource reconciled is
    already reported per resource, and repeating it here would put one problem
    in the operator's queue twice under two names. Everything below is invisible
    to any single resource, because it is a statement about how two of them
    relate.
    """

    by_id = {facet.id: facet for facet in facets}
    faults: list[str] = []

    for facet in facets:
        kinds = [claim.kind for claim in facet.claims]
        # Two providers of *different* kinds on one facet is normal -- an
        # internal answer and a public one are both DNS and legitimately differ.
        # Two of the same kind is a contradiction: only one can win, and which
        # one is decided by whichever reconciled last.
        for kind in sorted({kind for kind in kinds if kinds.count(kind) > 1}):
            faults.append(
                f"Two {kind} resources declare this name, so only one of them "
                "can be the answer."
            )

    # These two rules are statements about particular facets, so they name them.
    # A facet no provider supplies is not assembled, so a rule about it simply
    # does not apply -- which is the right answer for a question HQ cannot ask
    # rather than a fault to report.
    proxy = by_id.get("proxy")
    certificate = by_id.get("certificate")
    if proxy is None or certificate is None:
        return tuple(faults)

    serves = proxy.present
    if serves and not certificate.present:
        faults.append(
            "Something answers for this name but no declared certificate covers "
            "it, so HQ cannot show it is served over TLS."
        )
    if serves and origin is not None and not origin.known:
        faults.append(
            f"Ingress forwards to {origin.address}, which matches no host in the "
            "topology."
        )
    return tuple(faults)


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


def _locate(address: str, topology: dict[str, Any] | None) -> Origin:
    """Match a forwarding address to a machine, and if certain, a container.

    The topology maps an address to a machine; the container inventory says what
    is listening on it. Two sources because they answer different questions, and
    only one of them is observed: which machine an IP is has to be authored,
    while what is running on it is a fact a sweep can go and get.

    The container is named only when exactly one claims the port. Ambiguity is
    reported as silence -- a guess printed beside four facts reads as a fifth.
    """

    host_address, _, port = address.rpartition(":")
    for host in (topology or {}).get("hosts", ()):
        known = {
            host.get("id"),
            host.get("lan_ip"),
            host.get("ts_ip"),
            host.get("public_ip"),
        }
        if host_address not in known:
            continue
        host_id = host.get("id", "")
        claimed = _listening(host_id, port)
        if not claimed:
            # Nothing has swept this machine, so fall back to what the topology
            # says is on it. Ports there are prose -- "80, 443, 81" -- which is
            # why this is the fallback and not the answer.
            claimed = [
                container.get("id", "")
                for container in host.get("containers", ())
                if port
                and re.search(
                    rf"(?<!\d){re.escape(port)}(?!\d)",
                    str(container.get("ports", "")),
                )
            ]
        return Origin(
            address=address,
            host=host_id,
            container=claimed[0] if len(claimed) == 1 else "",
        )
    return Origin(address=address)


def _listening(host: str, port: str) -> list[str]:
    """Containers a controller last saw publishing one port on one machine.

    Structured ports, so "8081" cannot match "18081" and a container publishing
    three ports is found by any of them -- neither of which a regular expression
    over prose can promise.
    """

    if not host or not port.isdigit():
        return []
    wanted = int(port)
    return sorted(
        str(record.get("name", ""))
        for snapshot in ProviderInventory.objects.filter(kind="portainer.stack")
        for record in snapshot.records
        if record.get("host") == host
        and wanted in (record.get("ports") or [])
        and record.get("name")
    )


def _published_projects() -> dict[str, dict[str, str]]:
    """Hostname to the project that publishes there, for the ones that do.

    An annotation, never a requirement. Most of what an operator runs has no
    repository of its own, and a service that cannot name a project is not
    thereby incomplete.
    """

    found: dict[str, dict[str, str]] = {}
    for project in Project.objects.exclude(public_url="").only(
        "name", "slug", "public_url"
    ):
        hostname = urlparse(project.public_url).hostname
        if hostname:
            # Most recently updated wins a contested hostname: the model orders
            # by ``-updated_at``, and ``setdefault`` keeps the first. Two
            # projects claiming one name is a data problem, but picking the
            # stalest of them would be a worse answer than picking the freshest.
            found.setdefault(
                _normalise(hostname),
                {
                    "name": project.name,
                    "url": reverse("projects:detail", kwargs={"slug": project.slug}),
                },
            )
    return found


def _topology() -> dict[str, Any] | None:
    return (
        TopologySnapshot.objects.filter(pk="topology")
        .values_list("payload", flat=True)
        .first()
    )


# Shared with the domain view, so two projections of the same declaration
# cannot disagree about which names a certificate covers.
_resolved = resolved_spec


# ----- Machine-readable projection -------------------------------------------


def serialize_service(service: Service) -> dict[str, Any]:
    return {
        "hostname": service.hostname,
        "status": service.status,
        "facets": [
            {
                "id": facet.id,
                "label": facet.label,
                "present": facet.present,
                "state": facet.state,
                "resources": [claim.resource_key for claim in facet.claims],
            }
            for facet in service.facets
        ],
        "origin": (
            {
                "address": service.origin.address,
                "host": service.origin.host,
                "container": service.origin.container,
            }
            if service.origin
            else None
        ),
        "project": service.project["name"] if service.project else None,
        "faults": list(service.faults),
    }


def list_services() -> dict[str, Any]:
    """Every declared hostname and the state of its wiring."""

    items = [serialize_service(service) for service in service_catalog()]
    return {"items": items, "count": len(items)}


def get_service(hostname: str) -> dict[str, Any]:
    """One hostname, with the resources behind each facet named."""

    found = find_service(hostname)
    if found is None:
        raise NotFoundError(f"No service is declared for {hostname!r}.")
    return {"service": serialize_service(found)}
