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

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from django.urls import reverse

from control_plane.models import (
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
)
from control_plane.providers import (
    CONTAINER_KIND,
    PROVIDERS,
    NameContext,
    certificate_covers,
    service_facets,
    names_a_host,
    normalized_hostname,
)
from projects.models import Project

from .infrastructure import (
    NotFoundError,
    context_for_resolution,
    declared_machines,
    resolved_spec,
    resource_health,
)
from .naming import name_context
from .reach import UNKNOWN, Reach, reach_of
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
    # has three states, not two: declared, found, and absent. Collapsing the
    # middle one into absent reports a running service as missing, and offers to
    # build a second of what is already there.
    observed: "Running | None" = None
    # The machine whatever supplies this facet runs on. Held here so the card
    # links it once, whether the container is declared or merely observed.
    machine: Any = None
    # What HQ knows about this name. Held so ``declarable`` can ask each
    # provider whether it could actually supply it -- an offer that cannot work
    # is worse than no offer, and only the provider knows which is which.
    context: NameContext = field(default_factory=NameContext)

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
                if provider.facet == self.id
                and provider.seed is not None
                and not self._refused(provider)
            )
        )

    @property
    def unavailable(self) -> tuple[tuple[str, str], ...]:
        """``(label, reason)`` for providers this name rules out.

        Said rather than silently dropped. A `.homelab` service losing its
        Let's Encrypt option without explanation looks like a missing feature,
        and the sentence is what turns it into an answer -- it names the
        alternative that does work.
        """

        return tuple(
            sorted(
                (provider.label or kind, refused)
                for kind, provider in PROVIDERS.items()
                if provider.facet == self.id
                and provider.seed is not None
                and (refused := self._refused(provider))
            )
        )

    def _refused(self, provider) -> str:
        if provider.applies is None:
            return ""
        try:
            return provider.applies(self.context)
        except (KeyError, TypeError, ValueError):
            return ""

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
    network_mode: str
    host_address: str
    portainer_managed: bool
    connection_ref: str
    observed_at: Any
    # The declaration already watching this, when one is. A field rather than a
    # lookup, because a page renders a table of these and a property would be a
    # query per row -- and every row asks the same question of the same table.
    watcher: str = ""
    # Folded away on the machine's page. Still watched, still controllable --
    # this is about where it sits, not about whether HQ can act on it.
    hidden: bool = False

    @classmethod
    def of(
        cls,
        record: dict[str, Any],
        observed_at: Any,
        watchers: dict[tuple[str, str], tuple[str, bool]] | None = None,
    ) -> "Running":
        host = str(record.get("host", ""))
        name = str(record.get("name", ""))
        return cls(
            name=name,
            host=host,
            stack=str(record.get("stack", "")),
            image=str(record.get("image", "")),
            state=str(record.get("state", "")),
            status=str(record.get("status", "")),
            ports=tuple(
                int(port) for port in record.get("ports") or () if str(port).isdigit()
            ),
            network_mode=str(record.get("network_mode", "")),
            host_address=str(record.get("host_address", "")),
            portainer_managed=bool(record.get("portainer_managed")),
            connection_ref=str(record.get("connection_ref", "")),
            observed_at=observed_at,
            watcher=(watchers or {}).get((host, name), ("", False))[0],
            hidden=(watchers or {}).get((host, name), ("", False))[1],
        )

    @property
    def healthy(self) -> bool:
        return self.state == "running"

    @property
    def published(self) -> str:
        """The ports this publishes, or why that cannot be answered.

        A host-network container binds the machine's ports directly and Docker
        reports none for it, so an empty list means "not knowable from here"
        rather than "publishes nothing".
        """

        if self.ports:
            return ", ".join(str(port) for port in self.ports)
        if self.network_mode == "host":
            return "on the host network"
        return ""

    @property
    def token(self) -> str:
        """The handle adoption looks this record up by."""

        from .inventory import record_token

        return record_token(CONTAINER_KIND, (self.host, self.name))

    @property
    def verbs(self) -> tuple[str, ...]:
        """What it makes sense to ask of a container in this state.

        Offering all three always means offering Start to something already
        running, whose only outcome is Docker answering "already started" a
        minute later in a job result. The state is right here; the buttons
        should read it.
        """

        return ("stop", "restart") if self.healthy else ("start",)

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
        """Whether the address belongs to a machine HQ knows.

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

    @property
    def headline(self) -> str:
        """What to call whatever serves this, in one phrase.

        Here rather than in a template, because there are two templates and one
        fact. Phrased in each, they drift, and the board and the page disagree
        about the same origin.
        """

        if self.external:
            return self.operator or self.address
        return self.label

    @property
    def qualifier(self) -> str:
        """The caveat, when the headline needs one."""

        return "" if self.external or self.known else "unknown host"


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
    # resolves to. Not a declaration anybody makes -- a consequence of one.
    reach: Reach = UNKNOWN
    # ``(alias, claim)`` for the declarations that make those other names work.
    # Beside the service, never merged into its facets: merged, two CNAMEs read
    # as two records competing for one name.
    alias_claims: tuple[tuple[str, "Claim"], ...] = ()
    # Whether this operator keeps it at the top. A preference about a person,
    # never part of what HQ asks the controller to make true.
    pinned: bool = False


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
    def container(self) -> "Running | None":
        """The one container this service was found running in, if any.

        Held on the service rather than dug out of a facet by the template,
        because the page acts on it in a different place from where it reports
        it -- the controls belong beside the page's other actions, not inside a
        card that is describing something.
        """

        return next(
            (facet.observed for facet in self.facets if facet.observed), None
        )

    @property
    def zone_key(self) -> str:
        """The domain HQ manages that this name lives in, if it manages one.

        A service and a domain are different pages about overlapping things --
        one is a hostname and everything that has to be true for it to answer,
        the other is a zone and every record published in it. jseverino.com is
        both, and neither page had a way to reach the other.

        Matched through the provider that says it contains records, so the tie
        is the one the registry already declares rather than a second opinion
        about what a domain is.
        """

        for resource in ManagedResource.objects.filter(enabled=True):
            provider = PROVIDERS.get(resource.kind)
            if provider is None or not provider.contains:
                continue
            zone = str(resource.spec.get("zone", "")).strip().lower().rstrip(".")
            if zone and (self.hostname == zone or self.hostname.endswith(f".{zone}")):
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

    machines, targets = context_for_resolution()
    declared: dict[str, dict[str, list[Claim]]] = {}
    covering: list[tuple[str, frozenset[str], Claim]] = []
    origins: dict[str, str] = {}
    # Every address each name resolves to, so who can reach it can be derived
    # rather than recorded. Collected here because this is the one pass that
    # already reads every enabled declaration.
    answers: dict[str, list[str]] = {}

    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or not provider.facet or provider.hostnames is None:
            continue
        spec = _resolved(resource, targets)
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
            resolves_to = provider.answers(spec) if provider.answers else ()
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
            answers.setdefault(hostname, []).extend(resolves_to)

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
    return declared, covering, origins, aliases, alias_claims, machines, answers


def _certificates_in_use() -> dict[str, dict[str, Any]]:
    """The certificate each proxied name is actually served with.

    Observed, never declared. HQ does not hold the material for an internally
    signed certificate -- the CA that signs it is deliberately air-gapped --
    so it can never own one, and a page that only counts what HQ declares
    reported "no certificate covers this" for names that were being served over
    TLS all along.

    Read from the proxy's own sweep, because the proxy is the thing that
    chooses which certificate answers for a name.
    """

    found: dict[str, dict[str, Any]] = {}
    for snapshot in ProviderInventory.objects.filter(kind="npm.proxy_host"):
        for record in snapshot.records:
            certificate = record.get("certificate") or {}
            if not certificate.get("name"):
                continue
            for name in record.get("domain_names") or ():
                hostname = _normalise(str(name))
                if hostname:
                    found[hostname] = certificate
    return found


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
    # And the same site under the one prefix that conventionally means it.
    # A CNAME says "I am that name"; an address record says only where to go,
    # so `www.example.com` and `example.com` as two A records to one place look
    # like two services and are one site. Every other subdomain sharing an
    # address is a different service on one host -- mail and a quiz sitting on
    # the same cPanel are not each other -- so this is `www` and nothing else.
    for hostname in declared:
        apex = hostname.partition(".")[2]
        if not hostname.startswith("www.") or apex not in declared:
            continue
        if hostname in found or apex in found:
            continue
        here = _normalise(origins.get(hostname, ""))
        there = _normalise(origins.get(apex, ""))
        if here and here == there:
            found[hostname] = apex
    return found


def service_catalog(favorites: tuple[str, ...] = ()) -> tuple[Service, ...]:
    """Every hostname HQ declares, assembled from the resources that name it.

    ``favorites`` is the operator's own order for the handful they keep at the
    top. Applied here rather than in a template so every surface that lists
    services agrees about what comes first, and so the ordering never becomes
    a property of a Service -- it is a fact about a person, not a hostname.
    """

    declared, covering, origins, aliases, alias_claims, machines, answers = _declarations()
    estate = _Estate.read(covering, machines)
    by_target: dict[str, list[str]] = {}
    for alias, target in sorted(aliases.items()):
        by_target.setdefault(target, []).append(alias)
    found = tuple(
        _assemble(
            hostname,
            facets,
            estate,
            origins.get(hostname, ""),
            tuple(by_target.get(hostname, ())),
            tuple(alias_claims.get(hostname, ())),
            tuple(answers.get(hostname, ())),
        )
        for hostname, facets in sorted(declared.items())
    )
    if not favorites:
        return found
    from dataclasses import replace

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
    _, _, _, aliases, _, _, _ = _declarations()
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
    declared, covering, origins, aliases, alias_claims, machines, answers = _declarations()
    return _assemble(
        wanted,
        declared.get(wanted, {}),
        _Estate.read(covering, machines),
        origins.get(wanted, ""),
        tuple(alias for alias, target in sorted(aliases.items()) if target == wanted),
        tuple(alias_claims.get(wanted, ())),
        answers=tuple(answers.get(wanted, ())),
    )


def service_reading() -> dict[str, int]:
    """How many services there are, and how many are not fully wired."""

    catalog = service_catalog()
    return {
        "total": len(catalog),
        "incomplete": sum(1 for service in catalog if service.faults),
    }


@dataclass(frozen=True)
class MachineLink:
    """A machine named on a card, and the page for it.

    Built from the origin and the machines the caller already holds. Looking the
    machine up instead would be a catalogue read per service, which on a board
    is a catalogue read per row.
    """

    name: str
    role: str = ""

    @property
    def url(self) -> str:
        return reverse("control_plane:machine", kwargs={"name": self.name})


RUNTIME_FACET = "runtime"
DNS_FACET = "dns"


def _container_declarations() -> dict[tuple[str, str], Any]:
    """Container declarations, keyed by the machine and name they identify."""

    return {
        (resource.spec.get("host", ""), resource.spec.get("name", "")): resource
        for resource in ManagedResource.objects.filter(
            kind=CONTAINER_KIND, enabled=True
        )
    }


def machine_link(address: str) -> "MachineLink | None":
    """The machine an address belongs to, resolved the way a service resolves it.

    One resolution, so a page naming where something runs and a page naming what
    runs there cannot disagree about which machine that is.
    """

    machines = _machines()
    origin = _locate(address, machines)
    if not origin.host:
        return None
    return _machine_for(origin, machines)


def _machine_for(origin: "Origin | None", machines: "tuple[dict[str, Any], ...]"):
    """The machine whatever supplies this facet runs on."""

    if origin is None or not origin.host:
        return None
    role = next(
        (
            str(machine.get("role", ""))
            for machine in machines
            if str(machine.get("name", "")) == origin.host
        ),
        "",
    )
    return MachineLink(name=origin.host, role=role)


def _runtime_claim(
    origin: "Origin | None", containers: "dict[tuple[str, str], Any]"
) -> "Claim | None":
    """The declaration for the container this name is served from, if there is one.

    Matched on what the origin already resolved: a machine and a container on
    it. That is the same pair the declaration carries, so the two are the same
    thing recognised from opposite directions -- one authored, one observed.
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
    )


@dataclass(frozen=True)
class _Estate:
    """One reading of the world, shared by every service assembled from it.

    Five of these were positional arguments threaded through `_assemble` and
    repeated at each call site, so adding a sixth meant editing three places to
    say the same thing -- and the parameter list had grown past the point where
    anyone could tell which arguments were about this hostname and which were
    about the estate around it.

    Everything here is the same for every service in one build. What varies per
    name stays a parameter.
    """

    covering: list[tuple[str, frozenset[str], Claim]]
    projects: dict[str, dict[str, str]]
    machines: tuple[dict[str, Any], ...]
    containers: "dict[tuple[str, str], Any]"
    in_use: "_CertificatesInUse | None" = None

    @classmethod
    def read(cls, covering, machines) -> "_Estate":
        """The readings a catalogue needs, taken once."""

        return cls(
            covering=covering,
            projects=_published_projects(),
            machines=machines,
            containers=_container_declarations(),
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
) -> Service:
    covering = estate.covering
    projects = estate.projects
    machines = estate.machines
    containers = estate.containers
    in_use = estate.in_use
    origin = _locate(origin_address, machines) if origin_address else None
    context = name_context(hostname)
    # A container declaration names a machine and a container, not a hostname,
    # so nothing tied it to the name it serves -- the runtime card knew the
    # container and the resources table did not list it. The origin already
    # resolves both halves, which is the tie.
    runtime = _runtime_claim(origin, containers)
    facets = tuple(
        Facet(
            id=facet_id,
            label=label,
            claims=tuple(declared.get(facet_id, ()))
            + ((runtime,) if runtime and facet_id == RUNTIME_FACET else ())
            + tuple(
                claim
                for covered_facet, names, claim in covering
                if covered_facet == facet_id
                and certificate_covers(hostname, names)
            ),
            observed=_observed(facet_id, origin),
            machine=(
                _machine_for(origin, machines) if facet_id == RUNTIME_FACET else None
            ),
            context=context,
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
        faults=_faults(facets, origin, in_use, hostname),
        reach=reach_of(answers),
    )


# The provider whose inventory records are containers. Named once, here, because
# the runtime card is the one surface that has to know which sweep to read; every
# other reference to it in this module goes through this.


def container_watchers() -> dict[tuple[str, str], tuple[str, bool]]:
    """Which declaration watches which container, and whether it is folded away.

    Keyed on the identity the provider uses, so a container declared in HQ and
    the same container found by a sweep are recognised as one thing. Both facts
    come back together because a page rendering a table of containers asks both
    of every row, and asking twice is two queries for one join.
    """

    return {
        (resource.spec.get("host", ""), resource.spec.get("name", "")): (
            resource.key,
            bool(resource.spec.get("hidden")),
        )
        for resource in ManagedResource.objects.filter(
            kind=CONTAINER_KIND, enabled=True
        )
    }


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
    for snapshot in ProviderInventory.objects.filter(kind=CONTAINER_KIND):
        for record in snapshot.records:
            if (
                record.get("host") == origin.host
                and record.get("name") == origin.container
            ):
                return Running.of(record, snapshot.observed_at, container_watchers())
    return None


class _CertificatesInUse:
    """The proxy's certificates, read at most once and only if asked.

    Named for what it holds rather than for how it defers. A bare `get` on a
    generic `_Lazy` is indistinguishable from a dict lookup at every call site
    and in every tool that reads this code.
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
    """Wiring gaps -- the failures that exist only in the join.

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
    served_with = (
        in_use.covering(hostname)
        if serves and not certificate.present and in_use
        else None
    )
    if serves and not certificate.present and not served_with:
        faults.append(
            "Something answers for this name but nothing serves it over TLS -- "
            "no declared certificate covers it, and the proxy is not using one."
        )
    if serves and origin is not None and not origin.known:
        faults.append(
            f"Ingress forwards to {origin.address}, which HQ cannot match to "
            "any machine it knows."
        )
    return tuple(faults)


# Addresses reserved for writing about addresses. Nothing answers at one, so a
# name pointed there resolves to somewhere no packet arrives -- which reads as a
# working service on every board that only asks whether a record exists.
_DOCUMENTATION_RANGES = ("192.0.2.", "198.51.100.", "203.0.113.")


def _points_nowhere(origin: Origin | None, dns: "Facet | None" = None) -> str:
    """Why an address answers nothing, when that is knowable from the address.

    A parked name is a legitimate thing to have and an easy thing to forget, so
    HQ says which it is looking at rather than reporting the record as healthy
    because the record exists.

    Unless the provider answers on its own behalf. A proxied record puts the
    provider in front of the name: the address in it is a placeholder that no
    packet is meant to reach, and the redirect or page served there is the
    point. Reading that as "resolves to somewhere nothing answers" reported a
    working redirect as a fault, on the one record whose address was never
    supposed to mean anything.
    """

    if origin is None:
        return ""
    if _served_by_the_provider(dns):
        return ""
    address = origin.address.rpartition(":")[0] or origin.address
    if address.startswith(_DOCUMENTATION_RANGES):
        return (
            f"{address} is reserved for documentation, so this name resolves to "
            "somewhere nothing answers."
        )
    if address in {"0.0.0.0", "::"}:
        return f"{address} is not an address anything can be reached at."
    return ""


def _served_by_the_provider(dns: "Facet | None") -> bool:
    """Whether a DNS provider answers for this name rather than forwarding it.

    A proxied record means the provider terminates the connection and does
    whatever it was told -- redirect, cache, serve a page -- so the address in
    the record is a placeholder no packet is meant to reach.

    Read off the readings the claim already carries rather than fetched: the
    provider says this about itself in its own readout, and asking the database
    again would be a query per claim on a page that lists every service.
    """

    if dns is None:
        return False
    return any(
        reading.label == "Proxied"
        for claim in dns.claims
        for reading in claim.readings
    )


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


def _locate(address: str, machines: tuple[dict[str, Any], ...]) -> Origin:
    """Match a forwarding address to a machine, and if certain, a container.

    A machine declaration maps an address to a name; the container inventory
    says what is listening on it. Two sources because they answer different
    questions, and only one is observed: which machine an address belongs to is
    a thing HQ is told, while what is running on it is a fact a sweep goes and
    gets.

    The container is named only when exactly one claims the port. Ambiguity is
    reported as silence -- a guess printed beside four facts reads as a fifth.
    """

    # An address with no port is all host. `rpartition` puts the whole string
    # in its last element when the separator is absent, so splitting blind left
    # the host empty and every portless origin unmatchable -- a DNS answer
    # naming a machine HQ knows read as somewhere it had never heard of.
    host_address, separator, port = address.rpartition(":")
    if not separator:
        host_address, port = address, ""
    # A proxy forwarding to loopback is forwarding to itself: the request never
    # leaves the machine the ingress runs on. No machine is declared at
    # 127.0.0.1 -- every machine is -- so matching by address cannot answer it,
    # and the honest answer is the machine with something listening on that
    # port. Silence when more than one qualifies, for the reason below.
    if _is_loopback(host_address):
        found = [
            (machine, claimed)
            for machine in machines
            if (claimed := _listening(str(machine.get("name", "")), port))
        ]
        if len(found) == 1:
            machine, claimed = found[0]
            return Origin(
                address=address,
                host=str(machine.get("name", "")),
                container=claimed[0] if len(claimed) == 1 else "",
            )
        return Origin(address=address)
    for machine in machines:
        name = str(machine.get("name", ""))
        if host_address != name and host_address not in machine.get("addresses", ()):
            continue
        claimed = _listening(name, port)
        return Origin(
            address=address,
            host=name,
            container=claimed[0] if len(claimed) == 1 else "",
        )
    return Origin(address=address, host=_connected_machine(host_address))


def _is_loopback(address: str) -> bool:
    """Whether an address means "this machine", by the ranges rather than a name."""

    from .reach import network_of

    return network_of(address) == "loopback"


def _connected_machine(address: str) -> str:
    """A machine HQ holds a credential for, matched by where that credential points.

    A declaration is not the only thing that names a machine. A proxy forwarding
    to the cPanel host read as "unknown host" while the connections page listed
    that exact address under a name -- HQ knowing the machine well enough to log
    into it, and not well enough to say what it was called.
    """

    if not address:
        return ""
    for connection in ProviderConnection.objects.all():
        endpoint = connection.endpoint
        if not endpoint:
            continue
        if "://" in endpoint:
            endpoint = urlparse(endpoint).hostname or ""
        else:
            endpoint = endpoint.rpartition(":")[0] or endpoint
        if endpoint == address:
            return connection.connection_ref
    return ""


def _listening(host: str, port: str) -> list[str]:
    """Containers answering on one port of one machine, seen or declared.

    A container sharing the machine's network publishes nothing for Docker to
    report, so a sweep cannot find it by port and the declaration is the only
    thing that can say. Both are read, because a machine can be running one of
    each and the answer must not depend on which.
    """

    if not host or not port.isdigit():
        return []
    wanted = int(port)
    observed = {
        str(record.get("name", ""))
        for snapshot in ProviderInventory.objects.filter(kind=CONTAINER_KIND)
        for record in snapshot.records
        if record.get("host") == host
        and wanted in (record.get("ports") or [])
        and record.get("name")
    }
    declared = {
        str(spec.get("name", ""))
        for spec in ManagedResource.objects.filter(
            kind=CONTAINER_KIND, enabled=True
        ).values_list("spec", flat=True)
        if spec.get("host") == host and wanted in (spec.get("serves_ports") or [])
    }
    return sorted(observed | declared)


def _published_projects() -> dict[str, dict[str, str]]:
    """Hostname to the project that publishes there, for the ones that do.

    An annotation, never a requirement. Most of what an operator runs has no
    repository of its own, and a service that cannot name a project is not
    thereby incomplete.
    """

    return {
        hostname: {
            "name": project.name,
            "url": reverse("projects:detail", kwargs={"slug": project.slug}),
        }
        for hostname, project in projects_by_hostname().items()
    }


def service_url_for(public_url: str) -> str:
    """The service page for a published URL, when HQ manages that name.

    The reverse of the tie the service page makes, kept beside it so the two
    cannot come to disagree about which names HQ knows.
    """

    hostname = _normalise(urlparse(public_url or "").hostname or "")
    if not hostname or not find_service(hostname):
        return ""
    return reverse("control_plane:service", kwargs={"hostname": hostname})


def projects_by_hostname() -> dict[str, Project]:
    """The project publishing each name, keyed by that name.

    The one place that decides which project a service belongs to. Nothing
    points a project at infrastructure; a project that says where it is
    published has said which service it is, and reading that twice in two
    modules is two answers to one question.
    """

    found: dict[str, Project] = {}
    for project in Project.objects.exclude(public_url=""):
        hostname = urlparse(project.public_url).hostname
        if hostname:
            # Most recently updated wins a contested hostname: the model orders
            # by ``-updated_at``, and ``setdefault`` keeps the first. Two
            # projects claiming one name is a data problem, but picking the
            # stalest of them would be a worse answer than picking the freshest.
            found.setdefault(_normalise(hostname), project)
    return found








# Shared with the domain view, so two projections of the same declaration
# cannot disagree about which names a certificate covers.
_resolved = resolved_spec
_machines = declared_machines


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


def public_sites() -> tuple[tuple[str, str, str], ...]:
    """Names HQ publishes to the internet, as (label, sub, url).

    A dashboard link to a site is the site HQ already declares a public record
    for, so the list is whatever HQ is currently publishing rather than what it
    was publishing when somebody last edited a template.

    Read through the providers that say their effect is public, and through
    their own ``hostnames`` hook -- which returns nothing for the record types
    that carry policy, so a DMARC entry never arrives here looking like a site.
    """

    projects = _published_projects()
    found: dict[str, str] = {}
    targets: dict[str, str] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or not provider.public_effect:
            continue
        if provider.hostnames is None:
            continue
        try:
            names = tuple(provider.hostnames(resource.spec))
            origin = provider.origin(resource.spec) if provider.origin else ""
        except (KeyError, TypeError, ValueError):
            continue
        for name in names:
            hostname = _normalise(name)
            # A wildcard is a rule about names, not a name anything answers at.
            if hostname and names_a_host(hostname) and "*" not in hostname:
                found.setdefault(
                    hostname, projects.get(hostname, {}).get("name", "")
                )
                targets.setdefault(hostname, _normalise(origin))
    # A name whose target is another name here is the same site reached a
    # second way. The board folds those in, and a list that unfolds them shows
    # one site twice. An address with a port is where a name is served, not
    # another name for it.
    aliases = {
        hostname
        for hostname, target in targets.items()
        if target and ":" not in target and target != hostname and target in found
    }
    return tuple(
        (hostname, sub, f"https://{hostname}")
        for hostname, sub in sorted(found.items())
        if hostname not in aliases
    )
