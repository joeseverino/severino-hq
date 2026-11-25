"""HQ's playbook worker, absent: never seen once a playbook source exists, or not seen within its timer's interval.

Refresh and each run are refused while this stands, and this is what says how
to set the worker up, in docs/Deployment.md's order, since HQ cannot read why
the host declined to start one.
"""

from typing import Any

lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND

from .finding_model import Finding, FindingEstate, FindingRule, matter_key, opens
from .finding_steps import Do, Run
from .moments import when
from .security import Capability, Principal
from .tone import Tone
from .unit_findings import FAILED, HQ_HOST, NOT_INSTALLED, machine_units
lazy from .cadence import WorkerStanding, automation_standing
lazy from .infrastructure import declared_resources
lazy from .problems import About, problems_by_part
lazy from .routes import reverse

RULE = "automation-worker-absent"
# Who is told: whoever may read the Playbooks page. It is about no record or
# reading, so no subject or scope gates it.
_READ = (str(Capability.READ),)
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
_LOG = Run(
    label="Read why it stopped",
    command=f"sudo journalctl -u {AUTOMATION_UNIT} -n 50 --no-pager",
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


def _causes(estate: FindingEstate) -> tuple[str, ...]:
    """The findings the worker's absence follows from, on the machine whose
    units include the one that starts it: that unit failed, or it or what
    starts it is not set up."""

    causes = []
    for node, units in machine_units(estate):
        starts = tuple(unit for unit in units if AUTOMATION_UNIT in (unit.unit, unit.activates))
        if any(unit.unit == AUTOMATION_UNIT and unit.failed for unit in starts):
            causes.append(matter_key(FAILED, node.id))
        if any(unit.absent for unit in starts):
            causes.append(matter_key(NOT_INSTALLED, node.id))
    return tuple(causes)


def _absent(estate: FindingEstate) -> tuple[Finding, ...]:
    worker = _standing()
    if worker is None or not worker.cannot("refresh", now=estate.now):
        return ()
    # The page whose Refresh and runs it refuses, where its setup is shown.
    playbooks = opens("Open playbooks", reverse("control_plane:playbooks"))
    if worker.seen_at is None:
        return (
            Finding(
                rule=RULE,
                subject="",
                read_capabilities=_READ,
                title="Playbooks are not set up",
                severity=Tone.ATTENTION,
                explanation="Refresh and playbook runs are refused.",
                evidence=(("Last seen", "never"),),
                steps=(*_ENTRY, _START),
                primary=playbooks,
                follows=_causes(estate),
            ),
        )
    return (
        Finding(
            rule=RULE,
            subject="",
            read_capabilities=_READ,
            title="Playbooks stopped reporting",
            severity=Tone.SERIOUS,
            explanation="Refresh and playbook runs are refused.",
            evidence=(("Last seen", when(worker.seen_at, now=estate.now)),),
            steps=(_START, _LOG),
            primary=playbooks,
            follows=_causes(estate),
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
