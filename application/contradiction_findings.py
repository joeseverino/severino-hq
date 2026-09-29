"""Where two connections disagree about the same thing.

Each connection reports alone: the DNS provider its records, the proxy its
routes, Docker its containers, Access its applications. Each report can be
healthy while together they describe something that cannot work: a public
name answered by a machine that serves nothing for it, a route to a container
that is not running, an Access application guarding a name nothing answers.
These rules read what HQ already stores and walks (``application.paths``) and
poll nothing of their own.

Every finding names what each side says, so the fix is a choice between them,
and offers the declaration HQ can change to make them agree.


They are found while the topology is derived, which may read, and carried on
a node as a ``contradiction`` fact; the rules only read those facts, because
deriving findings costs no query.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from control_plane.names import normalized_hostname
from control_plane.providers import PROVIDERS

from .action_links import command_url
from .exposure import public_name
from .finding_model import Finding, FindingEstate, FindingRule, OperatorStep, Remedy, fact_values

# The fact a contradiction travels on, from the topology to the rule reading it.
FACT = "contradiction"

UPDATE_CAPABILITY = "infrastructure.resource.update"


def _update(key: str, label: str) -> Remedy:
    return Remedy(
        capability=UPDATE_CAPABILITY,
        target=key,
        label=label,
        effect="",
        url=command_url(UPDATE_CAPABILITY, key),
    )


def _claims(hostname: str, facet_ids: Iterable[str] = ()) -> tuple[Any, ...]:
    """The declarations taking part in a name's service, optionally by facet."""

    from .services import find_service

    service = find_service(hostname)
    if service is None:
        return ()
    wanted = set(facet_ids)
    return tuple(
        claim
        for facet in service.facets
        if not wanted or facet.id in wanted
        for claim in facet.claims
    )


def _declared(hostname: str, kinds: Iterable[str]) -> tuple[str, ...]:
    wanted = set(kinds)
    return tuple(dict.fromkeys(claim.resource_key for claim in _claims(hostname) if claim.kind in wanted))


def _subject(hostname: str) -> str:
    return f"service:{hostname}"


# ----- A public name that a machine answers but nothing on it serves ---------


def _answered_by_nothing() -> tuple[Finding, ...]:
    from .paths import path_to, routed_names

    # A machine where a container publishes a web port serves names HQ cannot
    # enumerate, so nothing there is called unserved.
    serving = {machine for (machine, _name), record in _containers_by_place().items()
               if {80, 443} & {int(port) for port in record.get("ports") or () if str(port).isdigit()}}
    found = []
    for name in routed_names():
        for route in path_to(name).routes:
            last = route.hops[-1] if route.hops else None
            if (
                not public_name(route) or last is None or last.step != "machine"
                or last.unread or route.unread or last.name in serving
            ):
                continue
            dns_kinds = [kind for kind, provider in PROVIDERS.items() if provider.public_effect]
            keys = _declared(name, dns_kinds)
            found.append(
                Finding(
                    rule="public-name-served-by-nothing",
                    subject=_subject(name),
                    title=f"{name} points at {last.name}, which serves nothing for it",
                    severity="attention",
                    explanation=(
                        f"Public DNS sends {name} to {last.name}, and every proxy and container "
                        f"reading HQ holds for {last.name} was read: none of them answers for "
                        "this name. Either the record is left over from something removed, or "
                        "the route that should serve it is missing."
                    ),
                    evidence=(("Name", name), ("Route", route.line), ("Machine", last.name)),
                    remedies=tuple(_update(key, f"Repoint {key}") for key in keys),
                    steps=(
                        OperatorStep(
                            label=f"Remove the public record for {name} if nothing should answer "
                            f"there, or add the proxy route on {last.name} that serves it."
                        ),
                    ),
                )
            )
    return tuple(found)


# ----- A route to a container that is not running ----------------------------


def _containers_by_place() -> dict[tuple[str, str], Mapping[str, Any]]:
    from control_plane.provider_adapters.portainer import CONTAINER_KIND

    from .facts import inventory_records

    return {
        (str(record.get("host", "")), str(record.get("name", ""))): record
        for _snapshot, record in inventory_records(CONTAINER_KIND)
    }


def _stopped_targets(route, containers) -> list[tuple[str, str, str]]:
    """``(machine, container, state)`` for each container the route ends at that is not running."""

    found = []
    machine = ""
    for hop in route.hops:
        if hop.step == "machine":
            machine = hop.name
        elif hop.step == "container":
            record = containers.get((machine, hop.name))
            state = str((record or {}).get("state", "") or "")
            if record is not None and state and state != "running":
                found.append((machine, hop.name, state))
    return found


def _routed_to_stopped() -> tuple[Finding, ...]:
    from .paths import path_to, routed_names

    containers = _containers_by_place()
    found = []
    for name in routed_names():
        for route in path_to(name).routes:
            for machine, container, state in _stopped_targets(route, containers):
                proxies = _declared(name, (kind for kind, provider in PROVIDERS.items() if provider.facet == "proxy"))
                found.append(
                    Finding(
                        rule="route-to-stopped-container",
                        subject=_subject(name),
                        title=f"{name} routes to {container} on {machine}, which is {state}",
                        severity="serious",
                        explanation=(
                            f"The route for {name} ends at the container {container}, and Docker "
                            f"on {machine} reports it {state}. Requests for the name fail until "
                            "it runs again or the route points at what replaced it."
                        ),
                        evidence=(("Name", name), ("Route", route.line), ("Container state", state)),
                        remedies=tuple(_update(key, f"Repoint {key}") for key in proxies),
                        steps=(
                            OperatorStep(
                                label=f"Start {container} on {machine}",
                                command=f"docker start {container}",
                            ),
                        ),
                    )
                )
    return tuple(found)


# ----- An Access application guarding a name nothing answers -----------------


def _gates_guarding_nothing() -> tuple[Finding, ...]:
    from control_plane.observations import OBSERVATIONS

    from .facts import inventory_records
    from .paths import routed_names

    routed = set(routed_names())
    found = []
    for spec in OBSERVATIONS.values():
        if not spec.restricts:
            continue
        for _snapshot, record in inventory_records(spec.kind):
            title = spec.title(record)
            for name in sorted({normalized_hostname(host) for host in spec.hostnames(record)} - routed - {""}):
                if "*" in name:
                    continue
                console = spec.console(record)
                found.append(
                    Finding(
                        rule="gate-guards-nothing",
                        subject=f"service:{name}",
                        title=f"{spec.label} {title} guards {name}, which no DNS record names",
                        severity="neutral",
                        explanation=(
                            f"{spec.label} {title} protects {name}, but no DNS record HQ reads "
                            "or declares answers for that name, so the gate guards nothing. "
                            "Remove the name from it, or it will quietly admit whoever it "
                            "allows the day the name is published again."
                        ),
                        evidence=(("Gate", f"{spec.label} {title}"), ("Name", name)),
                        steps=(
                            OperatorStep(
                                label=f"Remove {name} from {title}"
                                + (f" in the provider console: {console}" if console else "")
                            ),
                        ),
                    )
                )
    return tuple(found)


# ----- The internal and public answers disagree about where a name lives -----


def _split_horizon() -> tuple[Finding, ...]:
    from .paths import path_to, routed_names

    found = []
    for name in routed_names():
        routes = path_to(name).routes
        public = {route.machine for route in routes if public_name(route) and route.machine}
        internal = {route.machine for route in routes if not public_name(route) and route.machine}
        if not public or not internal or public & internal:
            continue
        rewrites = _declared(name, (kind for kind, provider in PROVIDERS.items() if provider.facet == "dns" and not provider.public_effect))
        found.append(
            Finding(
                rule="split-horizon-disagrees",
                subject=_subject(name),
                title=f"{name} lives on {', '.join(sorted(public))} publicly and {', '.join(sorted(internal))} inside",
                severity="attention",
                explanation=(
                    f"The public record for {name} leads to {', '.join(sorted(public))}; the "
                    f"internal record leads to {', '.join(sorted(internal))}. Inside and outside "
                    "then reach different machines under one name, which is either a migration "
                    "left half done or a rewrite pointing at the wrong place."
                ),
                evidence=tuple(("Route", route.line) for route in routes),
                remedies=tuple(_update(key, f"Repoint {key}") for key in rewrites),
                steps=(
                    OperatorStep(
                        label=f"Point the internal record for {name} at the machine the public one reaches, or the other way round."
                    ),
                ),
            )
        )
    return tuple(found)


# ----- A port published on a machine that no proxy fronts --------------------


def _unfronted_ports() -> tuple[Finding, ...]:
    from .containers import containers
    from .exposure import publicly_answering, routed_containers

    routed = routed_containers()
    found = []
    for item in containers():
        if not item.running.ports or item.serves or (item.machine.name, item.running.name) in routed:
            continue
        machine = item.machine
        answering = publicly_answering((*getattr(machine, "addresses", ()), getattr(machine, "address", "")))
        open_ports = sorted(port for port in item.running.ports if port in answering)
        if not open_ports:
            continue
        found.append(
            Finding(
                rule="published-port-unfronted",
                subject=f"machine:{machine.name}",
                title=f"{item.running.name} on {machine.name} answers the internet on port "
                f"{', '.join(str(port) for port in open_ports)}, and no name routes to it",
                severity="serious",
                explanation=(
                    "Docker publishes the port on every interface, the perimeter check saw it "
                    "answer from outside, and no proxy or DNS record HQ reads leads to it. "
                    "Whatever it serves is reachable by address alone, with nothing in front."
                ),
                evidence=(
                    ("Container", item.running.name),
                    ("Published", item.running.published),
                    ("Answered publicly", ", ".join(str(port) for port in open_ports)),
                ),
                steps=(
                    OperatorStep(
                        label=f"Bind {item.running.name}'s ports to 127.0.0.1 in its compose file "
                        f"(its page has the change: {item.url}#hardening), "
                        "or put a proxy route in front of it.",
                    ),
                ),
            )
        )
    return tuple(found)


# ----- A host serving a certificate other than the one HQ installed ---------


def _served_not_held() -> tuple[Finding, ...]:
    """A consumer the controller found serving a different certificate.

    The controller compares what each consumer serves with the fingerprint it
    installed; HQ holds and renews one certificate while the host hands out
    another, so a renewal changes nothing a browser sees.
    """

    from .infrastructure import enabled_resources

    found = []
    for resource in enabled_resources():
        consumers = (resource.status or {}).get("consumers") or []
        wrong = [
            item for item in consumers
            if isinstance(item, dict) and item.get("matches_expected") is False
        ]
        if not wrong:
            continue
        names = ", ".join(
            str(item.get("domain") or item.get("consumer") or "") for item in wrong
        )
        found.append(
            Finding(
                rule="served-certificate-not-held",
                subject=f"resource:{resource.key}",
                title=f"{names} serves a certificate other than {resource.key}",
                severity="serious",
                explanation=(
                    f"HQ holds and renews {resource.key}, and the controller found "
                    f"{names} serving a different certificate. A renewal changes "
                    "nothing a visitor sees until the host serves the one HQ installs."
                ),
                evidence=tuple(
                    (
                        str(item.get("consumer") or item.get("domain") or ""),
                        f"serves {str(item.get('fingerprint_sha256', ''))[:12]}…",
                    )
                    for item in wrong
                ),
                remedies=(
                    Remedy(
                        capability="infrastructure.reconcile",
                        target=resource.key,
                        label=f"Install {resource.key} again",
                        effect="",
                    ),
                ),
            )
        )
    return tuple(found)


_DETECTORS = (
    _answered_by_nothing,
    _routed_to_stopped,
    _gates_guarding_nothing,
    _split_horizon,
    _unfronted_ports,
    _served_not_held,
)


def add_contradiction_facts(nodes) -> None:
    """Find every contradiction and carry each on its subject's node.

    A subject with no node of its own (a name only a gate mentions) rides on
    the first node: the rules read every node, so where it rides only has to
    exist.
    """

    from dataclasses import replace

    if not nodes:
        return
    fallback = next(iter(nodes))
    for detect in _DETECTORS:
        for finding in detect():
            anchor = finding.subject if finding.subject in nodes else fallback
            node = nodes[anchor]
            nodes[anchor] = replace(node, facts=node.facts + ((FACT, _encoded(finding)),))


def _encoded(finding: Finding) -> str:
    return json.dumps(
        {
            "rule": finding.rule,
            "subject": finding.subject,
            "title": finding.title,
            "severity": finding.severity,
            "explanation": finding.explanation,
            "evidence": [list(pair) for pair in finding.evidence],
            "remedies": [[remedy.capability, remedy.target, remedy.label, remedy.url] for remedy in finding.remedies],
            "steps": [[step.label, step.command] for step in finding.steps],
        }
    )


def _decoded(value: str) -> Finding:
    item = json.loads(value)
    return Finding(
        rule=item["rule"],
        subject=item["subject"],
        title=item["title"],
        severity=item["severity"],
        explanation=item["explanation"],
        evidence=tuple(tuple(pair) for pair in item["evidence"]),
        remedies=tuple(
            Remedy(capability=capability, target=target, label=label, effect="", url=url)
            for capability, target, label, url in item["remedies"]
        ),
        steps=tuple(OperatorStep(label=label, command=command) for label, command in item["steps"]),
    )


def _raised(rule: str):
    def detect(estate: FindingEstate) -> tuple[Finding, ...]:
        found = {}
        for node in estate.nodes():
            for value in fact_values(node, FACT):
                finding = _decoded(value)
                if finding.rule == rule:
                    found.setdefault((finding.subject, finding.title), finding)
        return tuple(found[key] for key in sorted(found))

    return detect


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "served-certificate-not-held",
        "A host serving a certificate HQ does not hold",
        "serious",
        _raised("served-certificate-not-held"),
        operator_action="Install the certificate HQ holds again, or point the host at it.",
        no_help_reason="HQ installs its certificate; it cannot tell why the host serves another.",
    ),
    FindingRule(
        "public-name-served-by-nothing",
        "A public name nothing at its address serves",
        "attention",
        _raised("public-name-served-by-nothing"),
        operator_action="Remove the stale record, or add the route that should serve the name.",
        no_help_reason="Removing a record is destructive, so HQ offers to repoint it and leaves removal to its page.",
    ),
    FindingRule(
        "route-to-stopped-container",
        "A route to a container that is not running",
        "serious",
        _raised("route-to-stopped-container"),
        operator_action="Start the container, or repoint the route at what replaced it.",
        no_help_reason="HQ does not start containers; it can repoint a route it declares.",
    ),
    FindingRule(
        "gate-guards-nothing",
        "A gate on a name no record answers",
        "neutral",
        _raised("gate-guards-nothing"),
        operator_action="Remove the name from the gate.",
        no_help_reason="HQ reads Access applications and access lists but does not write them.",
    ),
    FindingRule(
        "split-horizon-disagrees",
        "Inside and outside reach different machines under one name",
        "attention",
        _raised("split-horizon-disagrees"),
        operator_action="Point the internal and public records at the same machine.",
        no_help_reason="HQ can repoint a record it declares, but cannot tell which side is right.",
    ),
    FindingRule(
        "published-port-unfronted",
        "A container answering the internet with nothing in front",
        "serious",
        _raised("published-port-unfronted"),
        operator_action="Bind the published ports to loopback, or front them with a proxy.",
        no_help_reason="HQ writes the compose change but does not edit compose files on a machine.",
    ),
)
