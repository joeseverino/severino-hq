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

from control_plane.models import ManagedResource, TopologySnapshot
from control_plane.providers import (
    PROVIDERS,
    SERVICE_FACETS,
    ProviderResolutionContext,
    certificate_covers,
    resolve_provider_spec,
)
from projects.models import Project

from .infrastructure import NotFoundError, resource_health
from .ui import ListRow


def _normalise(hostname: str) -> str:
    """One spelling of a name, so two declarations of it meet.

    A rewrite, a proxy host and a certificate are authored by hand in three
    places and will not agree on case or on the trailing dot. Names that differ
    only in those would otherwise appear as separate services, each missing
    whatever the other one had.
    """

    return hostname.strip().lower().rstrip(".")


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

    @property
    def present(self) -> bool:
        return bool(self.claims)

    @property
    def declarable(self) -> tuple[str, ...]:
        """Provider kinds that could supply this facet for a name that lacks it.

        Read from the registry rather than listed here, so the offer to add one
        appears for a provider declared long after this was written. Only kinds
        that can be seeded from a hostname: a certificate is chosen from those
        that already exist, not created by naming a service.
        """

        return tuple(
            sorted(
                kind
                for kind, provider in PROVIDERS.items()
                if provider.facet == self.id
                and not provider.covers
                and provider.seed is not None
            )
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
class Origin:
    """Where a request for this hostname is finally served."""

    address: str
    host: str = ""
    container: str = ""

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

    @property
    def url(self) -> str:
        return reverse("control_plane:service", kwargs={"hostname": self.hostname})

    @property
    def claims(self) -> tuple[Claim, ...]:
        return tuple(claim for facet in self.facets for claim in facet.claims)

    @property
    def status(self) -> str:
        """Worst news first, with a live failure outranking a wiring gap.

        A degraded resource is something that was working and is not. A fault is
        a name that was never fully wired. Both need attention and only one is
        an outage, so they are not the same colour.
        """

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


def service_catalog() -> tuple[Service, ...]:
    """Every hostname HQ declares, assembled from the resources that name it."""

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
            hostnames = tuple(_normalise(name) for name in provider.hostnames(spec))
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

    projects = _published_projects()
    return tuple(
        _assemble(hostname, facets, covering, origins.get(hostname, ""), projects, topology)
        for hostname, facets in sorted(declared.items())
    )


def find_service(hostname: str) -> Service | None:
    wanted = _normalise(hostname)
    return next(
        (service for service in service_catalog() if service.hostname == wanted), None
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
) -> Service:
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
        )
        for facet_id, label in SERVICE_FACETS
    )
    origin = _locate(origin_address, topology) if origin_address else None
    return Service(
        hostname=hostname,
        facets=facets,
        origin=origin,
        project=projects.get(hostname),
        faults=_faults(facets, origin),
    )


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
    # Indexing rather than getting is safe because ``_assemble`` builds a Facet
    # for every entry in SERVICE_FACETS whether or not anything supplies it -- a
    # missing key here would mean the facet had been removed from the vocabulary
    # entirely, at which point failing loudly is the correct outcome.
    serves = by_id["proxy"].present
    if serves and not by_id["certificate"].present:
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
    """Match a forwarding address to a topology host, and if certain, a container.

    The container is named only when exactly one on that host claims the port. A
    topology records ports as prose -- "80, 443, 81" -- so taking the first match
    is a guess, and a guess printed beside four facts reads as a fifth fact.
    Silence is the honest answer when two containers could both be it.
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
        claimed = [
            container.get("id", "")
            for container in host.get("containers", ())
            if port
            and re.search(
                rf"(?<!\d){re.escape(port)}(?!\d)", str(container.get("ports", ""))
            )
        ]
        return Origin(
            address=address,
            host=host.get("id", ""),
            container=claimed[0] if len(claimed) == 1 else "",
        )
    return Origin(address=address)


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


def _resolved(resource: ManagedResource, topology: dict[str, Any] | None) -> dict[str, Any]:
    """The spec as a controller would see it, falling back to the authored one.

    A certificate declares only a topology reference; the names it covers exist
    only after resolving that. Where resolution cannot happen -- no snapshot
    imported, a dangling reference -- the authored spec stands in and the
    certificate covers nothing. That surfaces as an uncovered name, which is
    exactly true: HQ cannot demonstrate that anything covers it.
    """

    try:
        return resolve_provider_spec(
            resource.kind,
            resource.spec,
            context=ProviderResolutionContext(topology=topology),
        )
    except (KeyError, TypeError, ValueError):
        return resource.spec


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
