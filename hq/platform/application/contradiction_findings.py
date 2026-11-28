"""Where two connections disagree about the same thing.

Each report can be healthy while together they describe something that cannot
work; the rules are docs/Derived-Facts.md#contradictions. Every finding names
what each side says, so the fix is a choice between them, and offers the
declaration HQ can change to make them agree.
"""

import shlex
from collections.abc import Iterable, Mapping
from typing import Any

from django.utils.text import Truncator

from hq.domains.control_plane.names import in_zone, normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.observations.portainer import CONTAINER_KIND

from .entity_links import entity_link
from .exposure import FRONT_DOOR_PORTS, front_door_names, listening_ports, public_name, publicly_answering
from .finding_model import (
    CANNOT_EDIT_COMPOSE,
    Finding,
    FindingEstate,
    FindingRule,
    distinct,
    fact_values,
    opens,
)
from .finding_steps import Advice, ByHand, Do, Does, Why
from .references import Kind
from .tone import Tone
from .ui import agreed, counted
lazy from . import exposure, zones
lazy from .containers import container_watchers, containers
lazy from .facts import inventory_records
lazy from .infrastructure import enabled_resources
lazy from .path_model import Route
lazy from .paths import path_to, routed_names
lazy from .services import find_service
lazy from .topology_estate import machine_nodes
lazy from .whereabouts import reads_containers_on

UPDATE_CAPABILITY = "infrastructure.resource.update"
# A service's wiring fault (``services._faults``), one fact per fault on its node.
SERVICE_FAULT = "service-fault"


def _update(key: str, *, intent: str) -> Does:
    return Does(UPDATE_CAPABILITY, key, intent=intent)


def _claims(hostname: str, facet_ids: Iterable[str] = ()) -> tuple[Any, ...]:
    """The declarations taking part in a name's service, optionally by facet."""

    service = find_service(hostname)
    if service is None:
        return ()
    wanted = set(facet_ids)
    return tuple(claim for facet in service.facets if not wanted or facet.id in wanted for claim in facet.claims)


def _declared(hostname: str, kinds: Iterable[str]) -> tuple[str, ...]:
    wanted = set(kinds)
    return tuple(dict.fromkeys(claim.resource_key for claim in _claims(hostname) if claim.kind in wanted))


def _subject(hostname: str) -> str:
    return Kind.SERVICE.of(hostname)


def _about(estate: FindingEstate, hostname: str, machine: str) -> dict[str, str]:
    """What a claim about ``hostname`` is about: its service, or where HQ
    lists no service of that name (a route it only read), the machine the
    name reaches, with the name as the part of it the claim is about."""

    if _subject(hostname) in {node.id for node in estate.nodes()}:
        return {"subject": _subject(hostname)}
    return {"subject": machine_nodes()(machine) or _subject(hostname), "part": hostname}


# ----- A public name that a machine answers but nothing on it serves ---------


def _answered_by_nothing(estate: FindingEstate) -> tuple[Finding, ...]:
    # Only a machine whose containers HQ reads can be said to serve nothing:
    # elsewhere (shared hosting, a bare server) what answers is unknown. A
    # machine where a container answers on a web port serves names HQ cannot
    # enumerate, so nothing there is called unserved either. On the host's
    # network a proxy publishes nothing, so what it listens on is what its
    # image exposes.
    read = containers()
    known = {item.machine.name for item in read}
    serving = {item.machine.name for item in read if FRONT_DOOR_PORTS & set(listening_ports(item))}
    found = []
    for name in routed_names():
        for route in path_to(name).routes:
            last = route.hops[-1] if route.hops else None
            if (
                not public_name(route)
                or last is None
                or last.step != "machine"
                or last.unread
                or route.unread
                or last.name not in known
                or last.name in serving
            ):
                continue
            dns_kinds = [kind for kind, provider in PROVIDERS.items() if provider.public_effect]
            keys = _declared(name, dns_kinds)
            found.append(
                Finding(
                    rule="public-name-served-by-nothing",
                    **_about(estate, name, last.name),
                    title=f"{name} leads nowhere",
                    severity=Tone.ATTENTION,
                    explanation=f"Nothing on {last.name} serves it.",
                    evidence=(
                        ("Name", entity_link("service", name)),
                        ("Path", route.line),
                        ("Proxy route", "none"),
                        ("Container on a web port", "none"),
                    ),
                    fixes=(
                        *(_update(key, intent="Change where it points") for key in keys),
                        Do(
                            f"Remove the public record for {name} if nothing should answer "
                            f"there, or add the proxy route on {last.name} that serves it."
                        ),
                    ),
                )
            )
    return distinct(found)


# ----- A route that ends where nothing runs ----------------------------------


def _containers_by_place() -> dict[tuple[str, str], Mapping[str, Any]]:
    return {
        (str(record.get("host", "")), str(record.get("name", ""))): record
        for _snapshot, record in inventory_records(CONTAINER_KIND)
    }


def _dead_ends(route, containers) -> list[tuple[str, str, str]]:
    """``(machine, container, state)`` for each container the route forwards
    to that is not running, and ``(machine, "", address)`` where it ends at a
    port no container publishes on a machine whose containers HQ reads."""

    found = []
    for forward in route.forwards:
        state = str((containers.get((forward.machine, forward.container)) or {}).get("state", "") or "")
        if forward.container and state and state != "running":
            found.append((forward.machine, forward.container, state))
    end = route.forwards[-1] if route.forwards else None
    if end is not None and end.machine and not end.container and reads_containers_on(end.machine):
        found.append((end.machine, "", end.address))
    return found


def _said(state: str) -> str:
    """Docker's word for a container's state, as a person says it."""

    return {"exited": "stopped", "dead": "stopped", "created": "not started"}.get(state, state)


def _start(machine: str, container: str) -> tuple[Advice, ...]:
    """Start a container HQ watches through its own command; one it does not
    watch is started by hand."""

    by_hand = ByHand.on(
        machine, f"Start {container} on {machine}", f"docker start {shlex.quote(container)}", Why.NOT_GRANTED
    )
    watcher = container_watchers().get((machine, container), ("", False))[0]
    if watcher:
        return (Does("infrastructure.resource.start", watcher, by_hand=next(iter(by_hand), None)),)
    return by_hand or (Do(f"Start {container} on {machine}"),)


def _routed_to_stopped(estate: FindingEstate) -> tuple[Finding, ...]:
    containers = _containers_by_place()
    found = []
    for name in routed_names():
        for route in path_to(name).routes:
            for machine, container, state in _dead_ends(route, containers):
                proxies = _declared(name, (kind for kind, provider in PROVIDERS.items() if provider.facet == "proxy"))
                if container:
                    title = f"{name} leads to {container} on {machine}, which is {_said(state)}"
                    said = f"Requests fail until {container} runs again."
                    shown = ("Container", _said(state))
                    steps: tuple[Advice, ...] = _start(machine, container)
                else:
                    title = f"{name} forwards to an unpublished port on {machine}"
                    said = "Requests fail unless something outside Docker answers there."
                    shown, steps = ("Forwards to", state), ()
                found.append(
                    Finding(
                        rule="route-to-stopped-container",
                        **_about(estate, name, machine),
                        title=title,
                        severity=Tone.SERIOUS if container else Tone.ATTENTION,
                        explanation=said,
                        evidence=(("Name", entity_link("service", name)), ("Path", route.line), shown),
                        fixes=(*(_update(key, intent="Change where it points") for key in proxies), *steps),
                    )
                )
    return distinct(found)


# ----- An Access application guarding a name nothing answers -----------------


def _gates_guarding_nothing(_estate: FindingEstate) -> tuple[Finding, ...]:
    routed = set(routed_names())
    # Only a name in a domain HQ reads can be said to have no record: a gate on
    # the provider's own login domain names nothing HQ could have seen.
    domains = zones.zone_names()
    found = []
    for spec in OBSERVATIONS.values():
        if not spec.restricts:
            continue
        for _snapshot, record in inventory_records(spec.kind):
            title = spec.title(record)
            for name in sorted({normalized_hostname(host) for host in spec.hostnames(record)} - routed - {""}):
                if "*" in name or not any(in_zone(name, zone) for zone in domains):
                    continue
                console = spec.console(record)
                found.append(
                    Finding(
                        rule="gate-guards-nothing",
                        subject=Kind.SERVICE.of(name),
                        title=f"{name} has an access rule but no DNS record",
                        severity=Tone.NEUTRAL,
                        explanation="Published again, it would get this rule unreviewed.",
                        evidence=(("Access rule", f"{spec.label} {title}"), ("Name", entity_link("service", name))),
                        fixes=(Do(f"Remove {name} from {title}", notes=((console,) if console else ())),),
                    )
                )
    return distinct(found)


# ----- The internal and public answers disagree about where a name lives -----


def _split_horizon(estate: FindingEstate) -> tuple[Finding, ...]:
    found = []
    for name in routed_names():
        routes = path_to(name).routes
        if apart := _machines_apart(routes):
            found.append(_split_horizon_finding(estate, name, routes, *apart))
    return distinct(found)


def _machines_apart(routes: tuple[Route, ...]) -> tuple[set[str], set[str]] | None:
    """The machines a name reaches from the internet and from home, when the
    two answers disagree; None when either is missing or they agree."""

    public = {route.answers.machine for route in routes if public_name(route) and route.answers.machine}
    internal = {route.answers.machine for route in routes if not public_name(route) and route.answers.machine}
    # Every machine a public request crosses: an edge that proxies on to
    # the machine the internal record names is a front, and deliberate.
    crossed = {hop.name for route in routes if public_name(route) for hop in route.hops if hop.step == "machine"}
    if not public or not internal or public & internal or internal <= crossed:
        return None
    return public, internal


def _dns_kinds(*, public: bool) -> tuple[str, ...]:
    """The kinds of DNS record that answer the internet, or only home."""

    return tuple(
        kind for kind, provider in PROVIDERS.items() if provider.facet == "dns" and provider.public_effect is public
    )


def _split_horizon_finding(
    estate: FindingEstate, name: str, routes: tuple[Route, ...], public: set[str], internal: set[str]
) -> Finding:
    return Finding(
        rule="split-horizon-disagrees",
        **_about(estate, name, min(public)),
        title=(
            f"{name} goes to {', '.join(sorted(public))} from the internet and {', '.join(sorted(internal))} from home"
        ),
        severity=Tone.ATTENTION,
        explanation="Unless meant, one record is out of date.",
        evidence=tuple(("Path", route.line) for route in routes),
        fixes=(
            *(_update(key, intent="Change the internal record") for key in _declared(name, _dns_kinds(public=False))),
            *(_update(key, intent="Change the public record") for key in _declared(name, _dns_kinds(public=True))),
            Do(f"Point the internal record for {name} at the machine the public one reaches, or the other way round."),
        ),
    )


# ----- A port published on a machine that no proxy fronts --------------------


def _unfronted_ports(_estate: FindingEstate) -> tuple[Finding, ...]:
    routed = exposure.routed_containers()
    found = []
    for item in containers():
        if not item.running.ports or item.serves or (item.machine.name, item.running.name) in routed:
            continue
        # The proxy every route enters through is what is in front, not a
        # thing with nothing in front of it.
        if front_door_names(item):
            continue
        machine = item.machine
        answering = publicly_answering((*machine.addresses, machine.address))
        open_ports = sorted(port for port in item.running.ports if port in answering)
        if not open_ports:
            continue
        found.append(
            Finding(
                rule="published-port-unfronted",
                subject=Kind.MACHINE.of(machine.name),
                part=item.running.name,
                title=f"{item.running.name} on {machine.name} is open to the internet on "
                f"{agreed(len(open_ports), 'port')} "
                f"{', '.join(str(port) for port in open_ports)}",
                severity=Tone.SERIOUS,
                explanation="No proxy is in front of it.",
                evidence=(
                    ("Container", item.link),
                    ("Published", item.running.published),
                    ("Open to the internet", ", ".join(str(port) for port in open_ports)),
                ),
                fixes=(
                    Do(
                        f"Bind {item.running.name}'s ports to 127.0.0.1 in its compose file, "
                        "or put a proxy route in front of it.",
                        notes=(f"{item.running.name}'s page shows the exact change.",),
                    ),
                ),
            )
        )
    return distinct(found)


# ----- A host serving a certificate other than the one HQ installed ---------


def _served_not_held(_estate: FindingEstate) -> tuple[Finding, ...]:
    """A consumer the controller found serving a different certificate.

    The controller compares what each consumer serves with the fingerprint it
    installed; HQ holds and renews one certificate while the host hands out
    another, so a renewal changes nothing a browser sees.
    """

    found = []
    for resource in enabled_resources():
        consumers = (resource.status or {}).get("consumers") or []
        wrong = [item for item in consumers if isinstance(item, dict) and item.get("matches_expected") is False]
        if not wrong:
            continue
        names = ", ".join(str(item.get("domain") or item.get("consumer") or "") for item in wrong)
        found.append(
            Finding(
                rule="served-certificate-not-held",
                subject=Kind.RESOURCE.of(resource.key),
                title=f"{names} serves a certificate other than {resource.key}",
                severity=Tone.SERIOUS,
                explanation="Visitors will not get HQ's renewals.",
                evidence=tuple(
                    (
                        str(item.get("consumer") or item.get("domain") or ""),
                        f"serves {Truncator(item.get('fingerprint_sha256', '')).chars(13)}",
                    )
                    for item in wrong
                ),
                fixes=(Does("infrastructure.resource.reconcile", resource.key, intent="Install it again"),),
            )
        )
    return distinct(found)


def _fault_said(hostname: str, faults: tuple[str, ...]) -> tuple[str, str]:
    """What is wrong with ``hostname``, by its first fault's first sentence,
    and what that fault says after it; the evidence says each of the others."""

    first, _stop, rest = faults[0].partition(". ")
    more = len(faults) - 1
    title = f"{hostname}: {first.rstrip('.')}" + (
        f", and {counted(more, 'more problem', 'more problems')}" if more else ""
    )
    return title, rest


def _wiring_incomplete(estate: FindingEstate) -> tuple[Finding, ...]:
    """A name whose declarations no single resource can see disagree: a name
    answered with no certificate covering it, a proxy pointing at a host HQ
    does not know, or two declarations of one kind for one name. Whether a
    resource reconciled is its own rule's, so one problem is never raised twice."""

    return tuple(
        wiring_incomplete(node.label, faults, url=node.url)
        for node in estate.nodes()
        if node.kind == "service" and (faults := fact_values(node, SERVICE_FAULT))
    )


def wiring_incomplete(hostname: str, faults: tuple[str, ...], *, url: str = "") -> Finding:
    """What is not set up right for ``hostname``, as its finding says it
    wherever it is drawn: the queue, and the service's own page."""

    title, explanation = _fault_said(hostname, faults)
    # A fault said in one sentence leaves what it means for the name to say.
    explanation = explanation or "It is not served as HQ declares it."
    return Finding(
        rule="service-wiring-incomplete",
        subject=_subject(hostname),
        severity=Tone.ATTENTION,
        title=title,
        explanation=explanation,
        evidence=tuple(("Also", fault) for fault in faults[1:]),
        primary=opens("Open service", url) if url else None,
    )


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "served-certificate-not-held",
        "Another certificate served",
        Tone.SERIOUS,
        _served_not_held,
        gathered=True,
        operator_action="Install HQ's certificate again, or point the site at it.",
        no_help_reason="HQ cannot tell why the site serves another certificate.",
        bars={Tone.SERIOUS: "a site serves a certificate HQ does not hold for it"},
    ),
    FindingRule(
        "public-name-served-by-nothing",
        "Public name leads nowhere",
        Tone.ATTENTION,
        _answered_by_nothing,
        gathered=True,
        operator_action="Remove the public record, or add the proxy route that serves the name.",
        no_help_reason="HQ cannot tell whether the name should still exist.",
        bars={Tone.ATTENTION: "a public name has no proxy route behind it"},
    ),
    FindingRule(
        "route-to-stopped-container",
        "Route to nothing running",
        Tone.SERIOUS,
        _routed_to_stopped,
        gathered=True,
        operator_action="Start what it leads to, or point the proxy route at what replaced it.",
        no_help_reason="HQ cannot start containers.",
        bars={
            Tone.SERIOUS: "a proxy route leads to a stopped container",
            Tone.ATTENTION: "a proxy route forwards to a port no container publishes",
        },
    ),
    FindingRule(
        "gate-guards-nothing",
        "Access rule for a removed name",
        Tone.NEUTRAL,
        _gates_guarding_nothing,
        gathered=True,
        operator_action="Remove the name from the rule.",
        no_help_reason="HQ cannot edit Access applications or access lists.",
        bars={Tone.NEUTRAL: "an access rule names a name that no longer exists"},
    ),
    FindingRule(
        "split-horizon-disagrees",
        "One name, two machines",
        Tone.ATTENTION,
        _split_horizon,
        gathered=True,
        operator_action="Point the internal and public records at the same machine.",
        no_help_reason="HQ cannot tell which one is right.",
        bars={Tone.ATTENTION: "a name's internal and public records point at different machines"},
    ),
    FindingRule(
        "published-port-unfronted",
        "Container open to the internet",
        Tone.SERIOUS,
        _unfronted_ports,
        gathered=True,
        operator_action="Bind the published ports to 127.0.0.1, or put a proxy route in front of them.",
        no_help_reason=CANNOT_EDIT_COMPOSE,
        bars={Tone.SERIOUS: "a container's port is open to the internet with no proxy in front"},
    ),
    FindingRule(
        "service-wiring-incomplete",
        "Service not fully set up",
        Tone.ATTENTION,
        _wiring_incomplete,
        operator_action="Change the service's records on its page so they agree.",
        no_help_reason="HQ cannot tell which of the records is meant.",
        bars={Tone.ATTENTION: "a service's records disagree about how it is reached"},
    ),
)
