"""HQ's playbook worker, absent: never seen once a playbook source exists, or not seen within its timer's interval.

No scan is asked for and each run is refused while this stands, and this is what says how
to set the worker up, in docs/Deployment.md's order, since HQ cannot read why
the host declined to start one.
"""

from typing import Any

lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND

from .finding_model import Finding, FindingEstate, FindingRule, opens
from .finding_steps import Do, Run
from .security import Principal
from .tone import Tone
from .unit_findings import HQ_HOST, WORKER_READ, stopped_reporting, unit_causes
lazy from .cadence import WorkerStanding, automation_standing
lazy from .infrastructure import declared_resources
lazy from .problems import About, problems_by_part
lazy from .routes import reverse

RULE = "automation-worker-absent"
# The unit that starts the worker on HQ's machine (deploy/systemd).
AUTOMATION_UNIT = "severino-hq-automation.service"
# The host's configuration.
HOST_CONFIGURATION = "/etc/severino-hq/host.json"

# As deploy/host.example.json shows it: the grant is one of the file's own,
# and reads that repository's contents.
_ENTRY = (
    Run(label="On HQ's machine, open its configuration", command=f"sudoedit {HOST_CONFIGURATION}"),
    Do(
        "Add this, naming the repository your playbooks are in and, for a private one, the grant that reads it",
        value='"automation": {"repository": "OWNER/REPOSITORY", "grant": "GRANT"}',
    ),
)
_START = Run(
    label="Start the worker, which names anything still missing",
    command=f"sudo {HQ_HOST} automation",
)


def worker_problems(principal: Principal) -> tuple[dict[str, Any], ...]:
    """That HQ's playbook worker is absent, as the open problem
    ``principal`` may read, for a page about a playbook's work
    (``partials/_problems.html``). It is about no record with a page, so it is
    found by its queue item's key (``findings.finding_key``)."""

    key = f"finding:{RULE}:"
    return tuple(item for _part, item in problems_by_part({RULE: About(keys=(key,))}, principal))


def _standing() -> WorkerStanding | None:
    """HQ's playbook worker's standing, or None for an HQ that never had a
    playbook source and never saw a worker: nobody set automation up there,
    so its absence is nothing to raise."""

    worker = automation_standing()
    if worker.seen_at is None and not any(resource.kind == SOURCE_KIND for resource in declared_resources()):
        return None
    return worker


def _absent(estate: FindingEstate) -> tuple[Finding, ...]:
    worker = _standing()
    if worker is None or not worker.cannot("scan", now=estate.now):
        return ()
    # The page whose scans and runs it holds, where its setup is shown.
    playbooks = opens("Open playbooks", reverse("control_plane:playbooks"))
    if worker.seen_at is None:
        return (
            Finding(
                rule=RULE,
                subject="",
                read_capabilities=WORKER_READ,
                title="Playbooks are not set up",
                severity=Tone.ATTENTION,
                explanation="Playbook scans and runs are refused.",
                evidence=(("Last seen", "never"),),
                steps=(*_ENTRY, _START),
                primary=playbooks,
                follows=unit_causes(estate, AUTOMATION_UNIT),
            ),
        )
    return (
        stopped_reporting(
            estate,
            worker.seen_at,
            rule=RULE,
            unit=AUTOMATION_UNIT,
            title="Playbooks stopped reporting",
            explanation="Playbook scans and runs are refused.",
            severity=Tone.SERIOUS,
            primary=playbooks,
            steps=(_START,),
        ),
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
            Tone.SERIOUS: "playbooks stopped reporting",
            Tone.ATTENTION: "playbooks are not set up",
        },
    ),
)
