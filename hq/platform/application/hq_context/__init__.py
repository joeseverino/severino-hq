"""HQ as a subject: what it runs, where, how it is doing, what it can see and
what is wrong with it.

The page about HQ and the ``hq`` resource are this one projection. Nothing in
it is configured: ``release``, ``hq_self``, ``work`` and ``sight`` derive each
part, and a reading that could not be taken is said, never filled in.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from hq.platform.application.labels import dotted

from ..integration_specs import ListRead, ResourceSpec
from ..projection import page_of, projection_scope
from ..references import Kind
from ..resources import EmptyQuery
from ..tone import Tone
from .band import Tile
from .release_part import Release, release, release_json, release_tile, repository as repository_of
from .sight_part import KEYLESS, HqSight, Sees, sight, sight_json, sight_tile
from .work_part import Renderer, Work, WorkUnit, work, work_json
lazy from .. import hq_self, request_context
lazy from ..entity_links import entity_link
lazy from ..estate_records import Problem
lazy from ..findings import estate_findings, finding_key
lazy from ..hq_self import hq_hostnames, own_machine, serving, site_label
lazy from ..problems import About, grouped, problems_by_part
lazy from ..request_path import request_path
lazy from ..security import Capability, Principal


@dataclass(frozen=True, slots=True)
class HQContext:
    label: str
    status: tuple[str, str]
    release: Release
    machine: Any = None
    container: Any = None
    work: Work = field(default_factory=Work)
    sight: HqSight = field(default_factory=HqSight)
    problems: tuple[Mapping[str, Any], ...] = ()
    # The part of the page each problem is about, beside it in ``problems``.
    parts: tuple[str, ...] = ()
    request_path: Any = None

    @property
    def problems_in(self) -> dict[str, tuple[Mapping[str, Any], ...]]:
        """The problems about each part of the page, by part."""

        return grouped(zip(self.parts, self.problems, strict=True))

    @property
    def tiles(self) -> tuple[Tile, ...]:
        """The summary band, each figure in the tone of the worse of its own
        reading and the problems about its part."""

        problems = self.problems_in
        return tuple(
            _worse(tile, problems.get(part, ()))
            for part, tile in (
                (RELEASE, release_tile(self.release)),
                (WHERE, _runs_tile(self)),
                (REACH, _reach_tile(self)),
                (SIGHT, sight_tile(self.sight)),
            )
            if tile
        )


def hq_context(request: Any = None, *, principal: Principal) -> HQContext:
    """HQ as the page and the resource say it to ``principal``, for ``request`` when there is one."""

    with projection_scope(seed=serving(request) if request is not None else None):
        return _context(request, principal)


def _context(request: Any, principal: Principal) -> HQContext:
    container, machine = _where_it_runs()
    path = request_path(request) if request is not None else None
    found = _problems(machine, container, principal, reached=path is not None)
    problems = tuple(item for _, item in found)
    return HQContext(
        label=site_label() or "HQ",
        status=status(problems),
        release=release(container),
        machine=machine,
        container=container,
        work=work(),
        sight=sight(),
        problems=problems,
        parts=tuple(part for part, _ in found),
        request_path=path,
    )


def _where_it_runs() -> tuple[Any, Any]:
    container = hq_self.own_container()
    return container, container.machine if container is not None else own_machine()


# ----- Problems ------------------------------------------------------------------


def hq_problems(principal: Principal) -> tuple[Mapping[str, Any], ...]:
    """The open problems about HQ that ``principal`` may read: the ones this
    record lists, which HQ's own service's health reads too."""

    container, machine = _where_it_runs()
    return tuple(item for _, item in _problems(machine, container, principal, reached=False))


RELEASE, WHERE, REACH, SIGHT = "release", "where", "reach", "sight"


def _problems(
    machine: Any, container: Any, principal: Principal, *, reached: bool
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """The queue's open items about HQ that ``principal`` may read, each with the part of the page it
    belongs under: the repository under the release, the machine and
    container under where it runs, its hostnames under how it was reached (or
    where it runs, when no request says how), the controller under what it
    can see."""

    urls = [machine.url if machine is not None else ""]
    if container is not None:
        urls.append(container.url)
        if container.project is not None:
            urls.append(container.project.get_absolute_url())
    names = [entity_link("service", name).url for name in hq_hostnames()]
    # A controller has no page of its own, so its findings are found by key.
    keys = [
        finding_key(finding)
        for finding in estate_findings(principal=principal)
        if Kind.CONTROLLER.identity_in(finding.subject)
    ]
    repository = repository_of(container)
    return problems_by_part(
        {
            RELEASE: About(under=(repository.url,) if repository is not None else ()),
            WHERE: About(urls=urls if reached else urls + names, parts=_parts(container)),
            REACH: About(urls=names if reached else ()),
            SIGHT: About(keys=keys),
        },
        principal,
    )


def _parts(container: Any) -> tuple[str, ...]:
    """What an item about HQ's own container may be about: the container, or
    the image it runs, which a finding about the image names."""

    return (container.running.name, container.standing.label) if container is not None else ()


def status(problems: Iterable[Mapping[str, Any]]) -> tuple[Tone, str]:
    """How HQ is doing, in the tone of its worst problem someone has to act on,
    with how many of each kind are open: "1 urgent · 1 needs attention"."""

    found = counts(problems)
    if not found:
        return Tone.GOOD, "Running"
    return found[0][0], dotted(*(words for _, words in found))


def counts(problems: Iterable[Mapping[str, Any]]) -> tuple[tuple[Tone, str], ...]:
    """How many open problems of each tone someone has to act on, worst first,
    each in its words: ``(Tone.SERIOUS, "1 urgent")``."""

    tones = [tone for item in problems if (tone := Tone(item["status"])).acted_on]
    return tuple((tone, f"{tones.count(tone)} {tone.urgency.lower()}") for tone in Tone if tone in tones)


def _worse(tile: Tile, problems: Iterable[Mapping[str, Any]]) -> Tile:
    acted_on = (tone for item in problems if (tone := Tone(item["status"])).acted_on)
    return replace(tile, tone=Tone.worst((tile.tone, *acted_on)))


def _runs_tile(found: HQContext) -> Tile | None:
    if found.machine is None:
        return None
    container = found.container
    detail = dotted(container.running.name, container.uptime) if container is not None else ""
    healthy = container is not None and container.running.state.running
    return Tile(
        "Runs on", found.machine.name, detail, found.machine.url, Tone.GOOD if healthy else Tone.ATTENTION, "server"
    )


def _reach_tile(found: HQContext) -> Tile | None:
    path = found.request_path
    if path is None:
        return None
    contradicted = bool(path.findings)
    return Tile(
        "Reached through",
        path.connection.channel.label,
        path.proof,
        tone=Tone.ATTENTION if contradicted else Tone.GOOD,
        icon="network",
    )


def _where_json(found: HQContext) -> dict[str, Any]:
    container = found.container
    if container is None:
        return {"machine": found.machine.name if found.machine is not None else None, "container": None}
    runtime = container.runtime or {}
    posture = container.posture
    return {
        "machine": container.machine.name,
        "container": container.address,
        "uptime": container.uptime,
        "health": runtime.get("health") or (check.said if (check := container.running.check) else ""),
        "restarts": runtime.get("restart_count"),
        "posture": {"met": posture.met, "measured": posture.measured},
        "services": [job.unit for job in found.work.services],
    }


def serialize_hq(found: HQContext) -> dict[str, Any]:
    """HQ as every adapter returns it: its open problems are the items."""

    path = found.request_path
    problems = [
        {
            "key": item["key"],
            "label": item["label"],
            "detail": item["detail"],
            "status": item["status"],
            "url": item["url"],
            "subject": item["subject"],
            "since": item["since"],
        }
        for item in found.problems
    ]
    return {
        **page_of(problems),
        "label": found.label,
        "status": found.status[0],
        "summary": found.status[1],
        "release": release_json(found.release),
        "where": _where_json(found),
        "reached": (
            {"channel": path.connection.channel.label, "proof": path.proof, "hostname": path.path.hostname}
            if path is not None
            else None
        ),
        "work": work_json(found.work),
        "sees": sight_json(found.sight),
    }


def read_hq(*, principal: Principal) -> dict[str, Any]:
    """The ``hq`` resource: HQ as its own page says it, for the calling request and its principal."""

    return serialize_hq(hq_context(request_context.current(), principal=principal))


__all__ = [
    "KEYLESS",
    "HQContext",
    "HqSight",
    "Release",
    "Renderer",
    "Sees",
    "Work",
    "WorkUnit",
    "hq_context",
    "read_hq",
    "serialize_hq",
]


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "hq",
            "HQ",
            "HQ itself: its release and branch, machine and container, background jobs, "
            "what it reads and open problems.",
            Capability.READ,
            listing=ListRead(read_hq, EmptyQuery, Problem),
            web_route="hq_self",
            pass_principal=True,
        ),
    )
