"""Each entity page's relationships, less what the page already says.

A page renders some relations as its own sections: a service its path, a
machine its containers table and Docker bands, a container its band and how it
runs. Listed again under Relationships, each would be said twice. Which ones a
page renders is its layout, so each page's rule is here once, derived from the
relation and reading registries rather than typed as phrases.
"""

from __future__ import annotations

from typing import Any, Iterable

from control_plane.observations import OBSERVATIONS
from control_plane.observations.portainer import IMAGE_KIND, RUNTIME_KIND, STACK_KIND, VOLUME_KIND

from .relationships import Relationships, relationships_for
from .security import Principal
from .topology import RELATIONS


def for_service(service: Any, sections: Iterable[Any], *, principal: Principal) -> Relationships:
    """Less what the path names (what it runs on, what declares it, and each
    kind of part a hop on it shows) and the access list when it has a section.
    Only the kinds a hop shows: a name with no certificate hop still needs
    Relationships to name its certificate."""

    hops = [hop for route in service.path.routes for hop in route.hops]
    steps = {hop.step for hop in hops}
    on_the_path = {
        facet
        for facet, present in (
            ("dns", "dns" in steps),
            ("proxy", "ingress" in steps),
            ("runtime", bool(steps & {"container", "served", "origin", "hq"})),
            ("certificate", any(hop.certificate for hop in hops)),
        )
        if present
    }
    shown = [
        RELATIONS["runs_on"].phrase,
        RELATIONS["declared_by"].phrase,
        RELATIONS["contains"].inverse,
        *(spec.relation for spec in OBSERVATIONS.values() if spec.facet in on_the_path and spec.relation),
    ]
    if any(section.id == "access" for section in sections):
        shown.append(OBSERVATIONS["npm.access_list"].relation)
    return relationships_for(f"service:{service.hostname}", principal=principal).without(*shown)


def for_machine(machine: Any, sections: Iterable[Any], *, principal: Principal) -> tuple[Relationships, Relationships]:
    """``(every relation, the ones its section shows)``. The containers table is
    what it runs and the image each runs, "Names it answers" what it serves,
    the band what reaches it, and each Docker section the reading it renders;
    "Declared as" names a tailnet device declaration. The band's links read
    every relation."""

    whole = relationships_for(f"machine:{machine.name}", principal=principal)
    if machine.route_approval_key:
        whole = whole.without(RELATIONS["on_tailnet"].phrase)
    rendered = {kind for section in sections for kind in section.renders}
    if machine.containers:
        rendered.add(IMAGE_KIND)
    shown = whole.without(
        RELATIONS["runs"].phrase,
        RELATIONS["runs_on"].inverse,
        RELATIONS["reaches"].inverse,
        *(OBSERVATIONS[kind].relation for kind in rendered if kind in OBSERVATIONS),
    )
    return whole, shown


def for_container(found: Relationships) -> Relationships:
    """Less what its band says (the machine, the image), "Answers for" the
    names, and How it runs its compose file, mounts and how it is run."""

    shown = (OBSERVATIONS[kind] for kind in (IMAGE_KIND, RUNTIME_KIND, STACK_KIND, VOLUME_KIND))
    return found.without(
        RELATIONS["runs_on"].phrase,
        RELATIONS["declared_by"].inverse,
        *(spec.relation_to(by_address=False, by_container=True) for spec in shown),
    )
