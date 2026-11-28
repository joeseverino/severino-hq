"""HQ's playbook workers, one per privilege profile HQ has asked work of:
whether each reports, when it last took work, and the machines its logins
reach.

The host runs one worker for the read profile and one for each profile a
runner connection lends a login as, so one can stop while the others report.
The Playbooks page and the HQ page draw this one projection, and the finding
that one is absent (``automation_findings``) reads it too.
"""

from dataclasses import dataclass, replace

lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND

from .cadence import WorkerStanding, automation_profiles, automation_standing
from .projection import read_once
from .tables import Cell, DataTable, TableColumn
lazy from .entity_links import entity_link
lazy from .infrastructure import declared_resources
lazy from .playbook_reach import runner_reaches


@dataclass(frozen=True, slots=True)
class ProfileWorker:
    """The worker lent one profile's logins, as its arrivals and claims say."""

    profile: str
    standing: WorkerStanding


def profile_workers() -> tuple[ProfileWorker, ...]:
    """The worker of every profile HQ has asked work of, the read profile's
    first; none on an HQ that never had a playbook source and never heard a
    worker, where nobody set automation up."""

    return read_once("automation_workers", _workers)


def _workers() -> tuple[ProfileWorker, ...]:
    found = tuple(ProfileWorker(profile, automation_standing(profile)) for profile in automation_profiles())
    if all(worker.standing.seen_at is None for worker in found) and not any(
        resource.kind == SOURCE_KIND for resource in declared_resources()
    ):
        return ()
    return found


def reached_by_profile() -> dict[str, tuple[str, ...]]:
    """Each machine each profile's logins reach, as the newest scan said, A to Z."""

    found: dict[str, list[str]] = {}
    for machine, profile in sorted(runner_reaches()):
        found.setdefault(profile, []).append(machine)
    return {profile: tuple(machines) for profile, machines in found.items()}


def _reaches(machines: tuple[str, ...]) -> Cell:
    """Each machine a profile's logins reach, linked, on one line."""

    first, *rest = [Cell.of(entity_link("machine", name)) for name in machines] or [Cell()]
    return replace(first, parts=tuple(rest))


def workers_table(workers: tuple[ProfileWorker, ...]) -> DataTable:
    reached = reached_by_profile() if workers else {}
    columns = (
        TableColumn("Profile", kind="identifier", cell=lambda worker: Cell(worker.profile)),
        TableColumn("Reaches", drop=2, cell=lambda worker: _reaches(reached.get(worker.profile, ()))),
        TableColumn("State", kind="status", css="key-col", cell=lambda worker: Cell.state(worker.standing.state)),
        TableColumn("Last claim", kind="status", drop=1, cell=lambda worker: Cell(moment=worker.standing.claimed_at)),
    )
    return DataTable.of("Playbook workers", columns, workers, keeps_order=True)
