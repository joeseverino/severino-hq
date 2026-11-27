"""One of HQ's playbook workers, absent: never seen once a playbook source exists, or not seen within its timer's interval.

The read profile's worker absent, no scan is asked for and each run is
refused; another profile's, each run as that profile is. This says how to
set the worker up, in docs/Deployment.md's order, since HQ cannot read why
the host declined to start one.
"""

from typing import Any

from .finding_model import Finding, FindingEstate, FindingRule, matter_key, opens
from .finding_steps import ByHand, Do, Why
from .security import Principal
from .tone import Tone
from .unit_findings import HQ_HOST, WORKER_READ, stopped_reporting, unit_causes
lazy from .automation_runs import READ_PROFILE
lazy from .automation_workers import ProfileWorker, profile_workers
lazy from .problems import About, problems_of
lazy from .routes import reverse

RULE = "automation-worker-absent"
# The unit that starts the worker on HQ's machine (deploy/systemd).
AUTOMATION_UNIT = "severino-hq-automation.service"
# The host's configuration.
HOST_CONFIGURATION = "/etc/severino-hq/host.json"

# As deploy/host.example.json shows it: the grant is one of the file's own,
# and reads that repository's contents.
_ENTRY = (
    ByHand("On HQ's machine, open its configuration", f"sudoedit {HOST_CONFIGURATION}", Why.FIRST_INSTALL),
    Do(
        "Add this, naming the repository your playbooks are in and, for a private one, the grant that reads it",
        value='"automation": {"repository": "OWNER/REPOSITORY", "grant": "GRANT"}',
    ),
)
_START = ByHand("Start the worker, which names anything still missing", f"sudo {HQ_HOST} automation", Why.FIRST_INSTALL)


def worker_problems(principal: Principal) -> tuple[dict[str, Any], ...]:
    """That one of HQ's playbook workers is absent, as each open problem
    ``principal`` may read, for a page about a playbook's work
    (``partials/_problems.html``). It is about no record with a page, so it is
    found by its queue item's key (``findings.finding_key``)."""

    keys = tuple(matter_key(RULE, "", worker.profile) for worker in profile_workers())
    return problems_of(About(keys=keys), principal)


def _absent(estate: FindingEstate) -> tuple[Finding, ...]:
    """The read profile's worker absent, which leaves every playbook waiting;
    else each other profile's worker absent alone."""

    absent = [worker for worker in profile_workers() if worker.standing.cannot("", now=estate.now)]
    # The read profile's worker comes first, and its absence is every other's.
    if absent and absent[0].profile == READ_PROFILE:
        absent = absent[:1]
    return tuple(_worker_absent(estate, worker) for worker in absent)


def _worker_absent(estate: FindingEstate, worker: ProfileWorker) -> Finding:
    # The page whose scans and runs it holds, where its setup is shown.
    playbooks = opens("Open playbooks", reverse("control_plane:playbooks"))
    read = worker.profile == READ_PROFILE
    named = "Playbooks" if read else f"The {worker.profile} playbook worker"
    explanation = (
        "Playbook scans and runs are refused."
        if read
        else f"Playbook runs as the {worker.profile} profile are refused."
    )
    if worker.standing.seen_at is None:
        return Finding(
            rule=RULE,
            subject="",
            part=worker.profile,
            read_capabilities=WORKER_READ,
            title="Playbooks are not set up" if read else f"{named} has never reported",
            severity=Tone.ATTENTION,
            explanation=explanation,
            evidence=(("Last seen", "never"),),
            fixes=(*_ENTRY, _START) if read else (_START,),
            primary=playbooks,
            follows=unit_causes(estate, AUTOMATION_UNIT),
        )
    return stopped_reporting(
        estate,
        worker.standing.seen_at,
        rule=RULE,
        unit=AUTOMATION_UNIT,
        part=worker.profile,
        title=f"{named} stopped reporting",
        explanation=explanation,
        severity=Tone.SERIOUS,
        primary=playbooks,
    )


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "Playbooks not reporting",
        Tone.ATTENTION,
        _absent,
        gathered=True,
        operator_action=f"Add an automation entry to {HOST_CONFIGURATION}, then run {HQ_HOST} automation as root.",
        no_help_reason="HQ cannot change its own machine's setup.",
        bars={
            Tone.SERIOUS: "playbooks stopped reporting, or one profile's worker did",
            Tone.ATTENTION: "playbooks are not set up, or one profile's worker has never reported",
        },
    ),
)
