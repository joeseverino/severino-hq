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

from ..projection import complete, projection_scope
from ..tone import Tone
from .band import Tile
from .release_part import Release, release, release_json, release_tile, repository as repository_of
from .sight_part import KEYLESS, HqSight, Sees, sight, sight_json, sight_tile
from .work_part import Renderer, Work, WorkUnit, work, work_json
lazy from .. import hq_self, request_context, service_health
lazy from ..entity_links import entity_link
lazy from ..findings import finding_key
lazy from ..hq_self import hq_hostnames, own_machine, serving, site_label
lazy from ..problems import About, problems_by_part
lazy from ..request_path import request_path
lazy from ..security import Principal


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

        found: dict[str, list[Mapping[str, Any]]] = {}
        for part, item in zip(self.parts, self.problems, strict=True):
            found.setdefault(part, []).append(item)
        return {part: tuple(items) for part, items in found.items()}

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
    container = hq_self.own_container()
    machine = container.machine if container is not None else own_machine()
    path = request_path(request) if request is not None else None
    found = _problems(machine, container, principal, reached=path is not None)
    problems = tuple(item for _, item in found)
    return HQContext(
        label=site_label() or "HQ",
        status=_status(problems),
        release=release(container),
        machine=machine,
        container=container,
        work=work(),
        sight=sight(),
        problems=problems,
        parts=tuple(part for part, _ in found),
        request_path=path,
    )


# ----- Problems ------------------------------------------------------------------


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
        finding_key(finding) for finding in service_health.hq_findings() if finding.subject.startswith("controller:")
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


def _status(problems: Iterable[Mapping[str, Any]]) -> tuple[Tone, str]:
    """How HQ is doing, in the tone of its worst problem, with how many of each
    kind are open: "1 urgent · 1 needs attention"."""

    tones = [Tone(item["status"]) for item in problems]
    if not tones:
        return Tone.GOOD, "Running"
    counts = [(tone, tones.count(tone)) for tone in Tone if tone is not Tone.GOOD and tone in tones]
    summary = dotted(*(f"{count} {tone.urgency.lower()}" for tone, count in counts))
    return counts[0][0] if counts else Tone.ATTENTION, summary or "Needs attention"


def _worse(tile: Tile, problems: Iterable[Mapping[str, Any]]) -> Tile:
    acted_on = (tone for item in problems if (tone := Tone(item["status"])).acted_on)
    return replace(tile, tone=Tone.worst((tile.tone, *acted_on)))


def _runs_tile(found: HQContext) -> Tile:
    if found.machine is None:
        return Tile("Runs on", "Not known", "No machine HQ reads answers at HQ's addresses.", icon="server")
    container = found.container
    detail = dotted(container.running.name, container.uptime) if container is not None else "Its container is not known"
    healthy = container is not None and container.running.healthy
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
        "health": runtime.get("health") or container.running.check,
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
        **complete(problems),
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
