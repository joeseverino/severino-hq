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
lazy from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

from .exposure import FRONT_DOOR_PORTS, front_door_names, listening_ports, public_name, publicly_answering
from .finding_model import (
    CANNOT_EDIT_COMPOSE,
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    distinct,
    fact_values,
    on_machine,
    opens,
)
from .finding_steps import Do, told
from .references import Kind
from .tone import Tone
from .ui import counted, ended
lazy from . import exposure, zones
lazy from .containers import containers
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


def _update(key: str, *, intent: str) -> FindingRemedy:
    return FindingRemedy(capability=UPDATE_CAPABILITY, target=key, intent=intent)


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
                    title=f"{name} points at {last.name}, which serves nothing for it",
                    severity=Tone.ATTENTION,
                    explanation=(
                        f"Public DNS sends {name} to {ended(last.name)} No proxy answers for "
                        f"this name, and no container on {last.name} answers on a web port. "
                        "Either the record is left over from something removed, or the proxy "
                        "route that should serve it is missing."
                    ),
                    evidence=(("Name", name), ("Path", route.line), ("Machine", last.name)),
                    remedies=tuple(_update(key, intent="Change where it points") for key in keys),
                    steps=(
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
    """``(machine, container, state)`` for each container the route ends at
    that is not running, and ``(machine, "", address)`` where it forwards to a
    port no container publishes on a machine whose containers HQ reads."""

    found = []
    machine = forwarded = ""
    for hop in route.hops:
        if hop.step == "upstream":
            machine, forwarded = "", hop.name
        elif hop.step == "machine":
            machine = hop.name
        elif hop.step == "container":
            forwarded = ""
            record = containers.get((machine, hop.name))
            state = str((record or {}).get("state", "") or "")
            if record is not None and state and state != "running":
                found.append((machine, hop.name, state))
    if forwarded and machine and reads_containers_on(machine):
        found.append((machine, "", forwarded))
    return found


def _said(state: str) -> str:
    """Docker's word for a container's state, as a person says it."""

    return {"exited": "stopped", "dead": "stopped", "created": "not started"}.get(state, state)


def _routed_to_stopped(estate: FindingEstate) -> tuple[Finding, ...]:
    containers = _containers_by_place()
    found = []
    for name in routed_names():
        for route in path_to(name).routes:
            for machine, container, state in _dead_ends(route, containers):
                proxies = _declared(name, (kind for kind, provider in PROVIDERS.items() if provider.facet == "proxy"))
                if container:
                    title = f"{name} leads to {container} on {machine}, which is {_said(state)}"
                    said = f"fail until {container} runs again or the proxy route points at what replaced it."
                    shown = ("Container", _said(state))
                    start = on_machine(machine, f"docker start {shlex.quote(container)}")
                    steps = (told(f"Start {container} on {machine}", start),)
                else:
                    title = f"{name} forwards to {state} on {machine}, where no container publishes that port"
                    said = "fail unless something outside Docker answers there, or the proxy route points elsewhere."
                    shown, steps = ("Forwards to", state), ()
                found.append(
                    Finding(
                        rule="route-to-stopped-container",
                        **_about(estate, name, machine),
                        title=title,
                        severity=Tone.SERIOUS if container else Tone.ATTENTION,
                        explanation=f"Requests for {name} {said}",
                        evidence=(("Name", name), ("Path", route.line), shown),
                        remedies=tuple(_update(key, intent="Change where it points") for key in proxies),
                        steps=steps,
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
                        title=f"{spec.label} {title} protects {name}, which has no DNS record",
                        severity=Tone.NEUTRAL,
                        explanation=(
                            f"Nothing answers at {ended(name)} If the name is published again "
                            "later, this rule will apply to it without anyone reviewing it."
                        ),
                        evidence=(("Access rule", f"{spec.label} {title}"), ("Name", name)),
                        steps=(Do(f"Remove {name} from {title}", notes=((console,) if console else ())),),
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

    public = {route.machine for route in routes if public_name(route) and route.machine}
    internal = {route.machine for route in routes if not public_name(route) and route.machine}
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
        explanation=(
            "That is right only if you meant it. Otherwise the public record or the internal record is out of date."
        ),
        evidence=tuple(("Path", route.line) for route in routes),
        remedies=(
            *(_update(key, intent="Change the internal record") for key in _declared(name, _dns_kinds(public=False))),
            *(_update(key, intent="Change the public record") for key in _declared(name, _dns_kinds(public=True))),
        ),
        steps=(
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
                f"{'port' if len(open_ports) == 1 else 'ports'} "
                f"{', '.join(str(port) for port in open_ports)}",
                severity=Tone.SERIOUS,
                explanation="It answered a test from outside your network, with no proxy in front.",
                evidence=(
                    ("Container", item.running.name),
                    ("Published", item.running.published),
                    ("Open to the internet", ", ".join(str(port) for port in open_ports)),
                ),
                steps=(
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
                explanation=(
                    f"HQ renews {resource.key}, but {names} is serving a different "
                    "one. Visitors will not get renewals until it serves HQ's."
                ),
                evidence=tuple(
                    (
                        str(item.get("consumer") or item.get("domain") or ""),
                        f"serves {Truncator(item.get('fingerprint_sha256', '')).chars(13)}",
                    )
                    for item in wrong
                ),
                remedies=(
                    FindingRemedy(
                        capability="infrastructure.resource.reconcile",
                        target=resource.key,
                        intent="Install it again",
                    ),
                ),
            )
        )
    return distinct(found)


def _fault_title(hostname: str, faults: tuple[str, ...]) -> str:
    """What is wrong with ``hostname``, by its first fault's first sentence; the explanation says all of each."""

    first = faults[0].split(". ", 1)[0].rstrip(".")
    more = len(faults) - 1
    return f"{hostname}: {first}" + (f", and {counted(more, 'more problem', 'more problems')}" if more else "")


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

    return Finding(
        rule="service-wiring-incomplete",
        subject=_subject(hostname),
        severity=Tone.ATTENTION,
        title=_fault_title(hostname, faults),
        explanation=" ".join(faults),
        primary=opens("Open service", url) if url else None,
    )


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "served-certificate-not-held",
        "A site is serving a different certificate",
        Tone.SERIOUS,
        _served_not_held,
        gathered=True,
        operator_action="Install HQ's certificate again, or point the site at it.",
        no_help_reason="HQ cannot tell why the site is serving another certificate.",
        bars={Tone.SERIOUS: "a site serves a certificate other than the one HQ holds for it"},
    ),
    FindingRule(
        "public-name-served-by-nothing",
        "A public name that leads nowhere",
        Tone.ATTENTION,
        _answered_by_nothing,
        gathered=True,
        operator_action="Remove the public record, or add the proxy route that should serve the name.",
        no_help_reason="HQ cannot tell whether the name should still exist.",
        bars={Tone.ATTENTION: "a public name has no proxy route behind it"},
    ),
    FindingRule(
        "route-to-stopped-container",
        "A route to nothing running",
        Tone.SERIOUS,
        _routed_to_stopped,
        gathered=True,
        operator_action="Start what it leads to, or point the proxy route at what replaced it.",
        no_help_reason="HQ cannot start containers.",
        bars={
            Tone.SERIOUS: "a proxy route leads to a container that is not running",
            Tone.ATTENTION: "a proxy route forwards to a port no container publishes",
        },
    ),
    FindingRule(
        "gate-guards-nothing",
        "Access rule for a name that no longer exists",
        Tone.NEUTRAL,
        _gates_guarding_nothing,
        gathered=True,
        operator_action="Remove the name from the rule.",
        no_help_reason="HQ cannot edit Access applications or access lists.",
        bars={Tone.NEUTRAL: "an access rule names a name that no longer exists"},
    ),
    FindingRule(
        "split-horizon-disagrees",
        "One name, two different machines",
        Tone.ATTENTION,
        _split_horizon,
        gathered=True,
        operator_action="Point the internal and public records at the same machine.",
        no_help_reason="HQ cannot tell which one is right.",
        bars={Tone.ATTENTION: "a name's internal and public records point at different machines"},
    ),
    FindingRule(
        "published-port-unfronted",
        "A container is open to the internet",
        Tone.SERIOUS,
        _unfronted_ports,
        gathered=True,
        operator_action="Bind the published ports to 127.0.0.1, or put a proxy route in front of them.",
        no_help_reason=CANNOT_EDIT_COMPOSE,
        bars={Tone.SERIOUS: "a container publishes a port to the internet with no proxy in front"},
    ),
    FindingRule(
        "service-wiring-incomplete",
        "A service not fully set up",
        Tone.ATTENTION,
        _wiring_incomplete,
        operator_action="Change the service's records on its page so they agree.",
        no_help_reason="HQ cannot tell which of the records is meant.",
        bars={Tone.ATTENTION: "a service's records disagree about how it is reached"},
    ),
)
