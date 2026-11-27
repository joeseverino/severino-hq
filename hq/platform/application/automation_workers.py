"""HQ's playbook workers, one per privilege profile: whether each reports,
when it last took work, and the machines its logins reach.

The host runs one worker for the read profile and one for each profile a
runner connection lends a login as, so one can stop while the others report.
The Playbooks page and the HQ page draw this one projection, and the finding
that one is absent (``automation_findings``) reads it too.
"""

from dataclasses import dataclass

lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND

from .cadence import WorkerStanding, automation_standing
from .projection import read_once
from .tables import Cell, DataTable, TableColumn
lazy from .automation_runs import READ_PROFILE, runner_reaches
lazy from .entity_links import entity_link
lazy from .infrastructure import declared_resources


@dataclass(frozen=True, slots=True)
class ProfileWorker:
    """The worker lent one profile's logins, as its arrivals and claims say."""

    profile: str
    standing: WorkerStanding
    # Each machine its logins reach, as the newest scan said, A to Z.
    machines: tuple[str, ...]


def profile_workers() -> tuple[ProfileWorker, ...]:
    """Every profile's worker, the read profile's first; none on an HQ that
    never had a playbook source and never heard a worker, where nobody set
    automation up."""

    return read_once("automation_workers", _workers)


def _workers() -> tuple[ProfileWorker, ...]:
    reaches = runner_reaches()
    profiles = sorted({profile for _machine, profile in reaches} - {READ_PROFILE})
    found = tuple(
        ProfileWorker(
            profile,
            automation_standing(profile),
            tuple(sorted(machine for machine, each in reaches if each == profile)),
        )
        for profile in (READ_PROFILE, *profiles)
    )
    if all(worker.standing.seen_at is None for worker in found) and not any(
        resource.kind == SOURCE_KIND for resource in declared_resources()
    ):
        return ()
    return found


_COLUMNS = (
    TableColumn("Profile", kind="identifier", cell=lambda worker: Cell(worker.profile, readout=True)),
    TableColumn(
        "Reaches",
        drop=2,
        cell=lambda worker: Cell(lines=tuple(Cell.of(entity_link("machine", name)) for name in worker.machines)),
    ),
    TableColumn("State", kind="status", css="key-col", cell=lambda worker: Cell.state(worker.standing.state)),
    TableColumn("Last claim", kind="status", drop=1, cell=lambda worker: Cell(moment=worker.standing.claimed_at)),
)


def workers_table(workers: tuple[ProfileWorker, ...]) -> DataTable:
    return DataTable.of("Playbook workers", _COLUMNS, workers, keeps_order=True)
