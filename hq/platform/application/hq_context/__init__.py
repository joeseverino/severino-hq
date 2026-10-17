"""HQ as a subject: what it runs, where, how it is doing, what it can see and
what is wrong with it.

The page about HQ and the ``hq`` resource are this one projection. Nothing in
it is configured: the release is the build's own revision and the readings of
its image and repository, the machine and container are the ones
``hq_self`` derives, the work is the ``host.unit`` and ``host.render_status``
readings of the machine HQ's controller runs on, and what it sees is every
registered reading. A reading that could not be taken is said, never filled in.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..projection import projection_scope
from .release import Release, release, release_json, release_tile, repository as repository_of
from .sight import KEYLESS, Sees, Sight, sight, sight_json, sight_tile
from .tones import ATTENTION, GOOD, NEUTRAL, SERIOUS, Tile
from .work import Job, Renderer, Work, work, work_json


@dataclass(frozen=True, slots=True)
class HQContext:
    label: str
    status: tuple[str, str]
    release: Release
    machine: Any = None
    container: Any = None
    work: Work = field(default_factory=Work)
    sight: Sight = field(default_factory=Sight)
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


def hq_context(request: Any = None) -> HQContext:
    """HQ as the page and the resource say it, for ``request`` when there is one."""

    from ..hq_self import serving

    with projection_scope(seed=serving(request) if request is not None else None):
        return _context(request)


def _context(request: Any) -> HQContext:
    from ..hq_self import own_container, own_machine, site_label
    from ..request_path import request_path

    container = own_container()
    machine = container.machine if container is not None else own_machine()
    path = request_path(request) if request is not None else None
    found = _problems(machine, container, reached=path is not None)
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
# Worst first: the order a summary counts in and a tone escalates by.
_SEVERITY = (SERIOUS, ATTENTION, NEUTRAL)


def _problems(machine: Any, container: Any, *, reached: bool) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """The queue's open items about HQ, each with the part of the page it
    belongs under: the repository under the release, the machine and
    container under where it runs, its hostnames under how it was reached (or
    where it runs, when no request says how), the controller under what it
    can see."""

    from ..entity_links import entity_link
    from ..findings import finding_key
    from ..hq_self import hq_hostnames
    from ..problems import About, problems_by_part
    from ..service_health import hq_findings

    urls = [machine.url if machine is not None else ""]
    if container is not None:
        urls.append(container.url)
        if container.project is not None:
            urls.append(container.project.get_absolute_url())
    names = [entity_link("service", name).url for name in hq_hostnames()]
    # A controller has no page of its own, so its findings are found by key.
    keys = [finding_key(finding) for finding in hq_findings() if finding.subject.startswith("controller:")]
    repository = repository_of(container)
    return problems_by_part(
        {
            RELEASE: About(under=(repository.url,) if repository is not None else ()),
            WHERE: About(urls=urls if reached else urls + names),
            REACH: About(urls=names if reached else ()),
            SIGHT: About(keys=keys),
        }
    )


def _status(problems: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    """How HQ is doing, in the tone of its worst problem, with how many of each
    kind are open: "1 urgent · 1 needs attention"."""

    from ..decisions import urgency

    states = [str(item.get("status", "")) for item in problems]
    if not states:
        return GOOD, "Running"
    counts = [(state, states.count(state)) for state in _SEVERITY if state in states]
    summary = " · ".join(f"{count} {urgency(state).lower()}" for state, count in counts)
    return counts[0][0] if counts else ATTENTION, summary or "Needs attention"


def _worse(tile: Tile, problems: Iterable[Mapping[str, Any]]) -> Tile:
    states = {tile.tone, *(str(item.get("status", "")) for item in problems)}
    worst = next((state for state in _SEVERITY[:2] if state in states), tile.tone)
    return replace(tile, tone=worst)


def _runs_tile(found: HQContext) -> Tile:
    if found.machine is None:
        return Tile("Runs on", "Not known", "No machine HQ reads answers at HQ's addresses.", icon="server")
    container = found.container
    detail = f"{container.running.name} · {container.uptime}" if container is not None else "Its container is not known"
    healthy = container is not None and container.running.healthy
    return Tile("Runs on", found.machine.name, detail, found.machine.url, GOOD if healthy else ATTENTION, "server")


def _reach_tile(found: HQContext) -> Tile | None:
    path = found.request_path
    if path is None:
        return None
    contradicted = bool(path.findings)
    return Tile(
        "Reached through",
        path.connection.channel.label,
        path.proof,
        tone=ATTENTION if contradicted else GOOD,
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
    return {
        "items": [
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
        ],
        "count": len(found.problems),
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


def read_hq() -> dict[str, Any]:
    """The ``hq`` resource: HQ as its own page says it, for the calling request."""

    from .. import request_context

    return serialize_hq(hq_context(request_context.current()))


__all__ = [
    "KEYLESS",
    "HQContext",
    "Job",
    "Release",
    "Renderer",
    "Sees",
    "Sight",
    "Work",
    "hq_context",
    "read_hq",
    "serialize_hq",
]
