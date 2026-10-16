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
from datetime import datetime
from typing import Any

from django.conf import settings

from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND

from .projection import projection_scope, read_once
from .timestamps import moment
from .ui import counted

GOOD = "good"
ATTENTION = "attention"
SERIOUS = "serious"
NEUTRAL = "neutral"

# The group of readings no connection holds a credential for.
KEYLESS = "Needs no connection"

# The unit types that start other units on their own.
_STARTERS = (".timer", ".path")


@dataclass(frozen=True, slots=True)
class Tile:
    """One figure of the summary band."""

    label: str
    value: str
    detail: str = ""
    url: str = ""
    tone: str = NEUTRAL
    icon: str = ""


@dataclass(frozen=True, slots=True)
class Release:
    """What HQ runs: the commit it was built from, the image that carries it,
    and where its repository is against that."""

    revision: str
    source: str
    image: str = ""
    digest: str = ""
    built: datetime | None = None
    # Whether the GitHub App reads a signature on the digest that runs; None
    # where nothing was read about the digest.
    signed: bool | None = None
    vulnerabilities: str = ""
    vulnerabilities_note: str = ""
    repository: Any = None
    # ``(name, version)`` of each extension composed into this build, as the
    # running process admitted them.
    extensions: tuple[tuple[str, str], ...] = ()

    @property
    def short(self) -> str:
        return self.revision[:7]

    @property
    def commit_url(self) -> str:
        return f"{self.source}/commit/{self.revision}" if self.source and self.revision else ""

    @property
    def head(self) -> str:
        return str(((self.repository.head if self.repository is not None else None) or {}).get("sha", ""))

    @property
    def deployed(self) -> str:
        production = self.repository.production if self.repository is not None else None
        return str((production or {}).get("sha", ""))

    @property
    def behind(self) -> bool | None:
        """Whether the default branch has moved past production; None unread."""

        return self.repository.undeployed if self.repository is not None else None

    @property
    def runs_deployed(self) -> bool | None:
        """Whether this process is the commit production last deployed."""

        if not self.revision or not self.deployed:
            return None
        return self.deployed.startswith(self.revision) or self.revision.startswith(self.deployed)

    @property
    def standing(self) -> str:
        """Where the release stands, in a few words."""

        if self.behind:
            return f"{self.repository.default_branch or 'Its branch'} is ahead"
        if self.runs_deployed is False:
            return "Not what production last deployed"
        if self.behind is False:
            return "Up to date"
        return "Not known"

    @property
    def tone(self) -> str:
        if self.runs_deployed is False:
            return SERIOUS
        if self.behind:
            return ATTENTION
        return GOOD if self.behind is False else NEUTRAL


@dataclass(frozen=True, slots=True)
class Job:
    """One unit HQ ships, as its own work: what it runs, when and how it last
    ran, and when it runs next."""

    unit: str
    runs: str
    last_run: datetime | None
    result: str
    tone: str
    next_run: datetime | None = None


@dataclass(frozen=True, slots=True)
class Renderer:
    """One credentials renderer's account of its own runs."""

    name: str
    result: str
    tone: str
    attempted: datetime | None = None
    succeeded: datetime | None = None


@dataclass(frozen=True, slots=True)
class Work:
    """The ``host.unit`` and ``host.render_status`` readings, or why not."""

    jobs: tuple[Job, ...] = ()
    renderers: tuple[Renderer, ...] = ()
    read_at: datetime | None = None
    # Why the units could not be read, in a sentence; "" when they were.
    unread: str = ""
    renderers_unread: str = ""

    @property
    def services(self) -> tuple[Job, ...]:
        """What keeps running rather than running on a schedule: the unit that
        serves HQ is one of these."""

        return tuple(job for job in self.jobs if job.next_run is None and job.result == "Running")


@dataclass(frozen=True, slots=True)
class Sees:
    """One reading kind as HQ holds it."""

    kind: str
    label: str
    provider: str
    state: str
    state_label: str
    records: int
    observed_at: datetime | None
    stale: bool = False
    remedy: str = ""
    error: str = ""

    @property
    def read(self) -> bool:
        from .credential_sight import PARTIAL, READABLE

        return self.state in (READABLE, PARTIAL)


@dataclass(frozen=True, slots=True)
class Sight:
    """Every reading kind, by the connection provider that takes it."""

    groups: tuple[tuple[str, tuple[Sees, ...]], ...] = ()
    # Providers no connection reads, by label.
    unconnected: tuple[str, ...] = ()

    @property
    def rows(self) -> tuple[Sees, ...]:
        return tuple(row for _label, rows in self.groups for row in rows)

    @property
    def read(self) -> int:
        return sum(row.read for row in self.rows)

    @property
    def stale(self) -> int:
        return sum(row.stale for row in self.rows)

    @property
    def never(self) -> int:
        """Listed kinds no sweep has read yet."""

        from .credential_sight import NEVER_SWEPT

        return sum(row.state == NEVER_SWEPT for row in self.rows)

    @property
    def failing(self) -> int:
        """Kinds a connection tried and could not read; not-connected ones are not failures."""

        from .credential_sight import REFUSED, UNREADABLE

        return sum(row.state in (REFUSED, UNREADABLE) for row in self.rows)


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
            tile for tile in (_release_tile(self), _runs_tile(self), _reach_tile(self), _sees_tile(self)) if tile
        )


def hq_context(request: Any = None) -> HQContext:
    """HQ as the page and the resource say it, for ``request`` when there is one."""

    from .hq_self import serving

    with projection_scope(seed=serving(request) if request is not None else None):
        return _context(request)


def _context(request: Any) -> HQContext:
    from .hq_self import own_container, own_machine, site_label
    from .request_path import request_path

    container = own_container()
    machine = container.machine if container is not None else own_machine()
    problems = _problems(machine, container)
    return HQContext(
        label=site_label() or "HQ",
        status=_status(problems),
        release=_release(container),
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

    from .entity_links import entity_link
    from .findings import finding_key
    from .hq_self import hq_hostnames
    from .problems import problems_among
    from .service_health import hq_findings

    urls = [machine.url if machine is not None else ""]
    if container is not None:
        urls.append(container.url)
        if container.project is not None:
            urls.append(container.project.get_absolute_url())
    urls.extend(entity_link("service", name).url for name in hq_hostnames())
    # A controller has no page of its own, so its findings are found by key.
    keys = [finding_key(finding) for finding in hq_findings() if finding.subject.startswith("controller:")]
    repository = _repository(container)
    return problems_among(urls, keys=keys, under=(repository.url,) if repository is not None else ())


def _status(problems: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    states = {str(item.get("status", "")) for item in problems}
    if SERIOUS in states:
        return SERIOUS, "Has a problem"
    if states:
        return ATTENTION, "Needs attention"
    return GOOD, "Running"


# ----- Release -------------------------------------------------------------------


def _repository(container: Any) -> Any:
    """The repository HQ is built from, as the GitHub App reads it: the one its
    image names, else the one this build says it came from."""

    from .github_estate import repository_for

    if container is not None and container.standing.repository is not None:
        return container.standing.repository
    return repository_for(_source())


def _source() -> str:
    from .entity_links import web_url

    return web_url(str(getattr(settings, "SEVERINO_HQ_SOURCE", "") or ""))


def _release(container: Any) -> Release:
    from .plugins import installed_plugins

    standing = container.standing if container is not None else None
    build = standing.build if standing is not None else None
    return Release(
        revision=str(getattr(settings, "SEVERINO_HQ_REVISION", "") or ""),
        source=_source(),
        image=container.running.image if container is not None else "",
        digest=standing.digest if standing is not None else "",
        built=standing.built if standing is not None else None,
        signed=bool(build.get("signed")) if build is not None else None,
        vulnerabilities=standing.vulnerabilities if standing is not None else "",
        vulnerabilities_note=standing.vulnerabilities_note if standing is not None else "",
        repository=_repository(container),
        extensions=tuple((plugin.name, plugin.version) for plugin in installed_plugins()),
    )


# ----- Its own work --------------------------------------------------------------


def work() -> Work:
    """The units HQ ships and its credentials renderers, as last read."""

    return read_once("hq_context.work", _work)


def _work() -> Work:
    from .facts import stored_snapshots

    stored = stored_snapshots()
    units = next(iter(stored.get(UNIT_KIND, ())), None)
    renders = next(iter(stored.get(RENDER_STATUS_KIND, ())), None)
    jobs, unread = _jobs(units)
    renderers, renderers_unread = _renderers(renders)
    return Work(
        jobs=jobs,
        renderers=renderers,
        read_at=units.observed_at if units is not None and units.connected else None,
        unread=unread,
        renderers_unread=renderers_unread,
    )


def _unread(snapshot: Any, noun: str) -> str:
    """Why a reading of HQ's own machine says nothing, or ""."""

    if snapshot is None:
        return f"No controller has reported {noun} yet."
    if not snapshot.connected:
        return f"The controller on HQ's machine is not given {noun} to read."
    if not snapshot.reachable:
        reason = f": {snapshot.error}" if snapshot.error else "."
        return f"The controller could not read {noun}{reason}"
    if snapshot.error and not snapshot.records:
        return f"The controller's answer about {noun} could not be understood."
    return ""


def _jobs(snapshot: Any) -> tuple[tuple[Job, ...], str]:
    from .unit_findings import unit_state

    unread = _unread(snapshot, "HQ's background jobs")
    if unread:
        return (), unread
    units = [unit_state(record) for record in snapshot.records or () if isinstance(record, dict)]
    by_name = {unit.unit: unit for unit in units}
    started = {unit.activates for unit in units if unit.starter}
    jobs = [_scheduled(unit, by_name.get(unit.activates)) for unit in units if unit.starter]
    jobs.extend(_standing(unit) for unit in units if not unit.starter and unit.unit not in started)
    return tuple(sorted(jobs, key=lambda job: job.unit)), ""


def _runs(unit: str) -> str:
    """What a unit runs, in the words HQ has for it: the scheduled work an
    instance of the job template names, else the unit's own name."""

    from .scheduled_work import SCHEDULED

    instance = unit.partition("@")[2].rpartition(".")[0]
    return next((work.label for work in SCHEDULED if instance and work.name == instance), unit)


def _scheduled(starter: Any, target: Any) -> Job:
    """A timer or path, as the work it starts."""

    result, tone = _result(starter) if target is None or starter.absent else _result(target)
    return Job(
        unit=starter.unit,
        runs=_runs(starter.activates) if starter.activates else "Nothing",
        last_run=moment(starter.last_trigger_at) or (moment(target.started_at) if target is not None else None),
        result=result,
        tone=tone,
        next_run=moment(starter.next_elapse_at),
    )


def _standing(unit: Any) -> Job:
    result, tone = _result(unit)
    return Job(unit=unit.unit, runs=_runs(unit.unit), last_run=moment(unit.started_at), result=result, tone=tone)


def _result(unit: Any) -> tuple[str, str]:
    """How a unit's last run went, in words, and its tone."""

    if unit.absent:
        return unit.absent, SERIOUS
    if unit.failed:
        return f"Failed: {unit.result or 'unknown'}", SERIOUS
    if unit.active in ("activating", "reloading"):
        return "Running now", NEUTRAL
    if unit.active == "active" and unit.sub == "running":
        return "Running", GOOD
    if unit.condition == "no":
        return "Skipped: its condition did not hold", NEUTRAL
    if unit.result == "success":
        return "Succeeded", GOOD
    if unit.result:
        return f"Ended: {unit.result}", ATTENTION
    return ("Waiting", GOOD) if unit.starter else ("Not run since the machine started", NEUTRAL)


def _renderers(snapshot: Any) -> tuple[tuple[Renderer, ...], str]:
    from .render_status_findings import FAILURE_CLASSES, UNKNOWN_FAILURE, UNREAD_REASONS, rendering

    unread = _unread(snapshot, "how credentials were refreshed")
    if unread:
        return (), unread
    found = []
    for record in snapshot.records or ():
        if not isinstance(record, dict):
            continue
        run = rendering(record, snapshot.observed_at)
        if run.reason:
            result, tone = UNREAD_REASONS.get(run.reason, run.reason), ATTENTION
        elif run.failed:
            result, tone = f"Failed: {FAILURE_CLASSES.get(run.failure, UNKNOWN_FAILURE)}", SERIOUS
        else:
            result, tone = ("Refreshed" if run.outcome == "rendered" else "Already current"), GOOD
        found.append(Renderer(run.name, result, tone, moment(run.attempted_at), moment(run.confirmed_at)))
    return tuple(found), ""


# ----- What it can see -----------------------------------------------------------


def sight() -> Sight:
    """Every registered reading kind, by provider, as last read."""

    return read_once("hq_context.sight", _sight)


def _sight() -> Sight:
    from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
    from hq.domains.control_plane.observations import OBSERVATIONS

    from .credential_sight import NEVER_SWEPT, NOT_CONNECTED
    from .facts import stored_snapshots

    stored = {kind: rows[0] for kind, rows in stored_snapshots().items() if rows}
    groups: dict[str, list[Sees]] = {}
    for kind, spec in OBSERVATIONS.items():
        row = _sees(kind, spec, stored.get(kind))
        groups.setdefault(CONNECTION_LABELS.get(spec.provider, KEYLESS), []).append(row)
    # A provider that takes a connection and has none is said in one line; a
    # reading that needs no connection is always listed, read or not.
    shown = {
        label: tuple(rows)
        for label, rows in groups.items()
        if label == KEYLESS or any(row.state not in (NOT_CONNECTED, NEVER_SWEPT) for row in rows)
    }
    return Sight(
        groups=tuple(sorted(shown.items(), key=lambda group: (group[0] == KEYLESS, group[0]))),
        unconnected=tuple(sorted(label for label in groups if label not in shown)),
    )


def _sees(kind: str, spec: Any, row: Any) -> Sees:
    from .credential_sight import PARTIAL, READABLE, sight as sight_of
    from .freshness import freshness

    seen = sight_of(kind, spec.label, "reading", row, permissions=tuple(spec.requires))
    return Sees(
        kind=kind,
        label=spec.label,
        provider=spec.provider,
        state=seen.state,
        state_label=seen.state_label,
        records=seen.records,
        observed_at=seen.observed_at,
        # Only a reading that was taken can be out of date; a refused one says so.
        stale=seen.state in (READABLE, PARTIAL) and freshness(kind, seen.observed_at).stale,
        remedy=seen.remedy,
        error=seen.error,
    )


# ----- The summary band ----------------------------------------------------------


def _release_tile(found: HQContext) -> Tile:
    release = found.release
    return Tile(
        "Release",
        release.short or "Not stamped",
        release.standing if release.revision else "This build does not say which commit it is.",
        release.commit_url,
        release.tone if release.revision else NEUTRAL,
        "deploy",
    )


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


def _sees_tile(found: HQContext) -> Tile:
    from .routes import reverse

    seen = found.sight
    detail = " · ".join(
        part
        for part in (
            f"{seen.stale} out of date" if seen.stale else "",
            f"{seen.failing} could not be read" if seen.failing else "",
            f"{seen.never} never read" if seen.never else "",
        )
        if part
    )
    return Tile(
        "Sees",
        counted(seen.read, "reading"),
        detail or "All current",
        reverse("control_plane:connections"),
        ATTENTION if seen.stale or seen.failing or seen.never else GOOD,
        "eye",
    )


# ----- As a resource -------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _release_json(release: Release) -> dict[str, Any]:
    repository = release.repository
    return {
        "revision": release.revision,
        "source": release.source,
        "standing": release.standing,
        "image": release.image,
        "digest": release.digest,
        "built": _iso(release.built),
        "signed": release.signed,
        "vulnerabilities": release.vulnerabilities,
        "repository": repository.name if repository is not None else None,
        "default_branch": repository.default_branch if repository is not None else None,
        "head": release.head,
        "deployed": release.deployed,
        "behind": release.behind,
        "runs_deployed": release.runs_deployed,
        "extensions": [{"name": name, "version": version} for name, version in release.extensions],
    }


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


def _plain(row: Job | Renderer) -> dict[str, Any]:
    """A row of words and moments as JSON: every field, each moment in ISO 8601."""

    from dataclasses import asdict

    return {name: _iso(value) if isinstance(value, datetime) else value for name, value in asdict(row).items()}


def _work_json(work: Work) -> dict[str, Any]:
    return {
        "read_at": _iso(work.read_at),
        "unread": work.unread,
        "jobs": [_plain(job) for job in work.jobs],
        "credentials_unread": work.renderers_unread,
        "credentials": [_plain(item) for item in work.renderers],
    }


def _sight_json(seen: Sight) -> dict[str, Any]:
    return {
        "read": seen.read,
        "out_of_date": seen.stale,
        "could_not_read": seen.failing,
        "never_read": seen.never,
        "not_connected": list(seen.unconnected),
        "readings": [
            {
                "kind": row.kind,
                "label": row.label,
                "provider": provider,
                "state": row.state,
                "records": row.records,
                "observed_at": _iso(row.observed_at),
                "out_of_date": row.stale,
                "remedy": row.remedy,
                "error": row.error,
            }
            for provider, rows in seen.groups
            for row in rows
        ],
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
        "release": _release_json(found.release),
        "where": _where_json(found),
        "reached": (
            {"channel": path.connection.channel.label, "proof": path.proof, "hostname": path.path.hostname}
            if path is not None
            else None
        ),
        "work": _work_json(found.work),
        "sees": _sight_json(found.sight),
    }


def read_hq() -> dict[str, Any]:
    """The ``hq`` resource: HQ as its own page says it, for the calling request."""

    from . import request_context

    return serialize_hq(hq_context(request_context.current()))
