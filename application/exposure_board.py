"""The exposure page: every name by who can reach it, and the problems ranked by it.

One projection over what ``application.exposure`` derives. The names come from
the service catalogue, so a name is here exactly when it is a service anywhere
else in HQ; the problems are the container items the action queue already
carries, ranked by exposure × severity instead of by how many advisories an
image has.
"""

from __future__ import annotations

from dataclasses import dataclass
from control_plane.providers import PROVIDERS

from .action_links import command_url
from .container_attention import UPDATE_CAPABILITY
from .entity_links import entity_link
from .exposure import LABELS, LEVELS, OPEN, exposure_of_name, status_rank
from .service_context import Cell, ServiceSection
from .ui import Insight
from .workflow_contracts import ActionLink

@dataclass(frozen=True)
class ExposureBoard:
    counts: tuple[tuple[str, str, int], ...]
    problems: tuple[Insight, ...]
    sections: tuple[ServiceSection, ...]


def gate_links(service) -> tuple[ActionLink, ...]:
    """The declarations HQ can put a gate on for this name: any ingress whose
    provider states an ingress policy, updated through its own command."""

    found = []
    for facet in service.facets:
        for claim in facet.claims:
            provider = PROVIDERS.get(claim.kind)
            if provider is None or provider.ingress_policy is None:
                continue
            found.append(
                ActionLink(
                    "gate",
                    f"Put an access list in front of {claim.resource_key}",
                    "remote_write",
                    command_url(UPDATE_CAPABILITY, claim.resource_key),
                    capability=UPDATE_CAPABILITY,
                    target=claim.resource_key,
                    reason="Set its access list, so the proxy admits only whom the list allows.",
                )
            )
    return tuple(found)


def _name_rows(names, services) -> list[tuple[int, str, tuple[Cell, ...]]]:
    rows = []
    for name in names:
        exposure = exposure_of_name(name)
        worst = exposure.worst
        service = services.get(name)
        fixes = gate_links(service) if service is not None and exposure.level == OPEN else ()
        rows.append(
            (
                LEVELS.index(exposure.level),
                name,
                (
                    Cell.of(entity_link("service", name)),
                    Cell(exposure.label, muted=exposure.level != OPEN),
                    Cell(worst.line if worst else "", muted=True),
                    Cell("; ".join(worst.gates) if worst and worst.gates else ""),
                    Cell(fixes[0].label, fixes[0].url) if fixes else Cell(""),
                ),
            )
        )
    return rows


def _problems() -> tuple[Insight, ...]:
    """The container items, worst first by what their status already weighs."""

    from .container_attention import attention

    return tuple(
        sorted(
            (item for item in attention() if item.key.startswith(("container-advisory:", "container-posture:"))),
            key=lambda item: (status_rank(item.status), item.title),
        )
    )


def exposure_board() -> ExposureBoard:
    from .paths import routed_names
    from .services import service_catalog

    services = {service.hostname: service for service in service_catalog()}
    names = sorted({*routed_names(), *services})
    rows = sorted(_name_rows(names, services), key=lambda row: (row[0], row[1]))
    counts = tuple(
        (level, LABELS[level], sum(1 for row in rows if row[0] == LEVELS.index(level)))
        for level in LEVELS
    )
    section = ServiceSection(
        id="names",
        label="Names",
        columns=("Name", "Reachable from", "Route", "Gate", "Fix"),
        records=tuple(row[2] for row in rows),
    )
    return ExposureBoard(counts=counts, problems=_problems(), sections=(section,) if rows else ())
