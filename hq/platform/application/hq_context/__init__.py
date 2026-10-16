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
from dataclasses import dataclass, field
from typing import Any

from ..projection import projection_scope
from .release import Release, release, release_json, release_tile, repository as repository_of
from .sight import KEYLESS, Sees, Sight, sight, sight_json, sight_tile
from .tones import ATTENTION, GOOD, SERIOUS, Tile
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
    request_path: Any = None

    @property
    def tiles(self) -> tuple[Tile, ...]:
        return tuple(
            tile
            for tile in (release_tile(self.release), _runs_tile(self), _reach_tile(self), sight_tile(self.sight))
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
    problems = _problems(machine, container)
    return HQContext(
        label=site_label() or "HQ",
        status=_status(problems),
        release=release(container),
        machine=machine,
        container=container,
        work=work(),
        sight=sight(),
        problems=problems,
        request_path=request_path(request) if request is not None else None,
    )


# ----- Problems ------------------------------------------------------------------


def _problems(machine: Any, container: Any) -> tuple[Mapping[str, Any], ...]:
    """The queue's open items about HQ's machine, its container, its names, its
    controller and its repository."""

    from ..entity_links import entity_link
    from ..findings import finding_key
    from ..hq_self import hq_hostnames
    from ..problems import problems_among
    from ..service_health import hq_findings

    urls = [machine.url if machine is not None else ""]
    if container is not None:
        urls.append(container.url)
        if container.project is not None:
            urls.append(container.project.get_absolute_url())
    urls.extend(entity_link("service", name).url for name in hq_hostnames())
    # A controller has no page of its own, so its findings are found by key.
    keys = [finding_key(finding) for finding in hq_findings() if finding.subject.startswith("controller:")]
    repository = repository_of(container)
    return problems_among(urls, keys=keys, under=(repository.url,) if repository is not None else ())


def _status(problems: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    states = {str(item.get("status", "")) for item in problems}
    if SERIOUS in states:
        return SERIOUS, "Has a problem"
    if states:
        return ATTENTION, "Needs attention"
    return GOOD, "Running"


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
