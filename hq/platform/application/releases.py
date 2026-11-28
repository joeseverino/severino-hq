"""HQ's releases: production's deployments as GitHub keeps them, each with
what HQ read of itself once it went live (``release_snapshots``), read as the
``releases`` resource and drawn on its pages under ``/hq/``.

Nothing about a release is copied into HQ but that snapshot: its commit, when,
how it ended and who approved it are the GitHub reading's, so they are never
older than the last read.
"""

from collections.abc import Collection, Mapping, Sequence
from datetime import timedelta
from typing import Annotated, Any, TypedDict

from hq.domains.jobs.models import Job
from hq.platform.core.models import ReleaseSnapshot

from . import deploy_outcomes, release_pages
from .answer_fields import DateTime, PageUrl
from .describe import Describe
from .entity_links import web_url
from .integration_specs import DetailRead, ListRead, ResourceSpec
from .labels import human_bytes
from .projection import iso, page_of
from .record_errors import RecordNotFound
from .release_snapshots import TIMED, ReleaseReadings, UnitRun, loaded
from .resources import EmptyQuery
from .routes import reverse
from .security import Capability
from .tables import Cell, DataTable, TableColumn
from .ui import measured
lazy from . import hq_self
lazy from .hq_context.release_part import repository
lazy from .moments import Span

RELEASES = "releases"


class ReleaseItem(TypedDict):
    """One release of HQ: a deployment to production, as GitHub records it."""

    commit: Annotated[str, Describe("The commit it released, in full.")]
    url: PageUrl
    commit_url: Annotated[str, Describe("The commit on GitHub; empty where HQ's source is not known.")]
    outcome: Annotated[
        str, Describe("The outcome its deploy reported, as deploy/outcomes.json names it; empty for none.")
    ]
    outcome_said: Annotated[str, Describe("How it ended, in a few words: Live, Not deployed.")]
    check_failed: Annotated[bool, Describe("Whether a check its deploy ran failed.")]
    state: Annotated[str, Describe("Its deployment's latest state on GitHub: success, inactive once replaced.")]
    created_at: Annotated[str, DateTime(), Describe("When its deploy began, an ISO 8601 date-time.")]
    ended_at: Annotated[
        str | None, Describe("When its deploy reported how it ended, an ISO 8601 date-time; null before.")
    ]
    run_url: Annotated[str, Describe("The Deploy run that released it, on GitHub.")]
    approved_by: Annotated[str, Describe("The GitHub login that approved its run; empty where none did.")]
    image: Annotated[str, Describe("The digest-pinned image it deployed.")]
    readings_kept: Annotated[bool, Describe("Whether HQ kept what it read of itself once it went live.")]


class UnitRunRecord(TypedDict):
    unit: Annotated[str, Describe("The unit's name, as systemd has it.")]
    result: Annotated[str, Describe("How its last run ended, in systemd's word; empty while it had not run.")]
    started_at: Annotated[str | None, Describe("When its last run started; null unread.")]
    ended_at: Annotated[str | None, Describe("When its last run ended; null while it runs.")]
    seconds: Annotated[float | None, Describe("How long its last run took; null without both ends.")]


class ReadingsRecord(TypedDict):
    """What HQ read of its own container and units once the release went live."""

    memory: Annotated[int | None, Describe("Bytes its container used, as memory_basis counts them; null unread.")]
    memory_basis: Annotated[str, Describe("How memory was counted.")]
    cpu_percent: Annotated[float | None, Describe("Its CPU since it started, as a share of one core.")]
    started_at: Annotated[str | None, Describe("When its container started.")]
    runtime_read_at: Annotated[str | None, Describe("When its container was read.")]
    failed: Annotated[list[str], Describe("Every unit of HQ's that systemd held as failed.")]
    units: Annotated[list[UnitRunRecord], Describe("The controller, glance and drift units' last runs.")]
    units_read_at: Annotated[str | None, Describe("When the units were read.")]


class PreviousRecord(TypedDict):
    commit: Annotated[str, Describe("The release production ran before it.")]
    url: PageUrl
    readings: Annotated[ReadingsRecord | None, Describe("What HQ read of it; null where nothing was kept.")]


class ReleaseRecord(ReleaseItem):
    """One release whole: its readings beside the previous release's."""

    readings: Annotated[ReadingsRecord | None, Describe("What HQ read of itself; null where nothing was kept.")]
    pages: Annotated[
        list[release_pages.PageRecord], Describe("Each page its rehearsal drew, by path; empty where none was kept.")
    ]
    pages_unread: Annotated[str, Describe("Why its rehearsal drew no page; empty where it drew them or none was kept.")]
    verified: Annotated[list[dict[str, str]], Describe("Each check its deploy ran before deploying, and its end.")]
    previous: Annotated[PreviousRecord | None, Describe("The release production ran before it; null for none.")]


def _deployments() -> tuple[tuple[Mapping[str, Any], ...], str]:
    """Production's deployments of HQ's own repository, newest first, and the
    repository's address on GitHub."""

    found = repository(hq_self.own_container())
    return (found.releases, web_url(found.url)) if found is not None else ((), "")


def _first_live(deployments: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((item for item in deployments if deploy_outcomes.went_live(item)), None)


def live() -> Mapping[str, Any] | None:
    """The release production runs: the newest of its deployments that went
    live, None before one has."""

    return _first_live(_deployments()[0])


def _item(deployment: Mapping[str, Any], kept: Collection[str], source: str) -> dict[str, Any]:
    commit = str(deployment.get("sha") or "")
    return {
        "commit": commit,
        "url": reverse("releases:detail", args=[commit]),
        "commit_url": f"{source}/commit/{commit}" if source else "",
        "outcome": str(deployment.get("outcome") or ""),
        "outcome_said": deploy_outcomes.said(deployment)[0],
        "check_failed": bool(deployment.get("check_failed")),
        "state": str(deployment.get("state") or ""),
        "created_at": str(deployment.get("created_at") or ""),
        "ended_at": deployment.get("ended_at") or None,
        "run_url": web_url(deployment.get("url")),
        "approved_by": str(deployment.get("approved_by") or ""),
        "image": str(deployment.get("image") or ""),
        "readings_kept": commit in kept,
    }


def list_releases() -> dict[str, Any]:
    """HQ's releases, newest first."""

    deployments, source = _deployments()
    commits = [deployment.get("sha") for deployment in deployments]
    kept = set(ReleaseSnapshot.objects.filter(commit__in=commits).values_list("commit", flat=True))
    return page_of([_item(deployment, kept, source) for deployment in deployments])


def get_release(commit: str) -> dict[str, Any]:
    """One release by its commit, or the prefix of one, beside the release
    production ran before it."""

    deployments, source = _deployments()
    at = next((index for index, item in enumerate(deployments) if str(item.get("sha") or "").startswith(commit)), None)
    if not commit or at is None:
        raise RecordNotFound("No release of HQ GitHub keeps is that commit.")
    deployment = deployments[at]
    before = _first_live(deployments[at + 1 :])
    commits = [str(item.get("sha") or "") for item in (deployment, before) if item is not None]
    snapshots = {snapshot.commit: loaded(snapshot) for snapshot in ReleaseSnapshot.objects.filter(commit__in=commits)}
    found = _item(deployment, snapshots, source)
    taken = snapshots.get(found["commit"])
    previous, earlier = None, ""
    if before is not None:
        earlier = str(before.get("sha") or "")
        previous = {
            "commit": earlier,
            "url": reverse("releases:detail", args=[earlier]),
            "readings": _readings(snapshots.get(earlier)),
        }
    return {
        **found,
        "readings": _readings(taken),
        "pages": release_pages.rows(taken, snapshots.get(earlier)),
        "pages_unread": taken.pages_unread if taken is not None else "",
        "verified": [dict(step) for step in deployment.get("verified") or ()],
        "previous": previous,
    }


def _readings(taken: ReleaseReadings | None) -> dict[str, Any] | None:
    if taken is None:
        return None
    return {
        "memory": taken.memory,
        "memory_basis": taken.memory_basis,
        "cpu_percent": taken.cpu_percent,
        "started_at": iso(taken.started_at),
        "runtime_read_at": iso(taken.runtime_read_at),
        "failed": list(taken.failed),
        "units": [_run(run) for run in taken.units],
        "units_read_at": iso(taken.units_read_at),
    }


def _run(run: UnitRun) -> dict[str, Any]:
    took = run.took
    return {
        "unit": run.unit,
        "result": run.result,
        "started_at": iso(run.started_at),
        "ended_at": iso(run.ended_at),
        "seconds": took.total_seconds() if took is not None else None,
    }


# ----- The release page's comparison ------------------------------------------


def _ran(run: Mapping[str, Any] | None) -> Cell:
    """A unit's last run: how it ended, and how long it took."""

    if run is None or not run["result"]:
        return Cell("Not run")
    ended = Job.State.SUCCEEDED.label if run["result"] == "success" else f"Failed: {run['result']}"
    took = str(Span(timedelta(seconds=run["seconds"]))) if run["seconds"] is not None else ""
    return Cell(lines=(Cell(ended), Cell(took))) if took else Cell(ended)


def _measures(readings: Mapping[str, Any] | None) -> dict[str, Cell]:
    if readings is None:
        return {}
    runs = {run["unit"]: run for run in readings["units"]}
    return {
        "Memory": Cell(human_bytes(readings["memory"])) if readings["memory"] is not None else Cell(),
        "CPU since start": Cell(f"{measured(readings['cpu_percent'], '%', -1)} of one core")
        if readings["cpu_percent"] is not None
        else Cell(),
        "Failed units": Cell(", ".join(readings["failed"]) or "None"),
        **{unit: _ran(runs.get(unit)) for unit in TIMED},
    }


READINGS_ORDER = ("Memory", "CPU since start", "Failed units", *TIMED)


def comparison(release: Mapping[str, Any]) -> DataTable:
    """A release's readings beside the previous release's, a row each."""

    this = _measures(release["readings"])
    previous = release["previous"] or {}
    before = _measures(previous.get("readings"))
    rows = [(label, this.get(label, Cell()), before.get(label, Cell())) for label in READINGS_ORDER]
    columns = (
        TableColumn("Reading", kind="identifier", cell=lambda row: Cell(row[0], readout=row[0] in TIMED)),
        TableColumn("This release", kind="status", css="key-col", cell=lambda row: row[1]),
        TableColumn("Previous release", kind="status", cell=lambda row: row[2]),
    )
    return DataTable.of("Readings", columns, rows)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            RELEASES,
            "Releases",
            "HQ's releases to production, newest first: each commit, how it ended, its Deploy run, who approved"
            " it, and what HQ read of itself once it went live beside the release before it.",
            Capability.READ,
            listing=ListRead(list_releases, EmptyQuery, ReleaseItem),
            detail=DetailRead(get_release, ReleaseRecord),
            identifier="commit",
            web_route="releases:list",
        ),
    )
