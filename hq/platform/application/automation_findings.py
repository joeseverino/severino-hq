"""HQ's playbook worker, absent: never seen once a playbook source exists,
or not seen within its timer's interval (``cadence.automation_standing``). An
HQ where nobody set automation up raises nothing; Refresh and each run still
refuse there with the reason.

A scan or run asked of it then waits for nothing, so Refresh and each
playbook's run are refused while this stands, and this is what says how to
set the worker up. HQ cannot read why the host declined to start one (its
launcher says so in its own log), so the steps are the whole setup, in the
order docs/Deployment.md gives it, for any operator's host.
"""

from typing import Any

from .finding_model import Finding, FindingEstate, FindingRule, OperatorStep
from .moments import ago
from .security import Capability, Principal
lazy from .problems import About, problems_by_part

RULE = "automation-worker-absent"
# Who is told: whoever may read the Playbooks page. It is about no record or
# reading, so no subject or scope gates it.
_READ = (str(Capability.READ),)
# The unit that starts the worker on HQ's machine (deploy/systemd).
AUTOMATION_UNIT = "severino-hq-automation.service"
# The host's own program, as the unit runs it, and its configuration.
HQ_HOST = "/usr/local/lib/severino-hq/deploy/bin/hq-host"
HOST_CONFIGURATION = "/etc/severino-hq/host.json"

# As deploy/host.example.json shows it: the grant is one of the file's own,
# and reads that repository's contents.
_ENTRY = OperatorStep(
    label=(
        'On HQ\'s machine, add "automation": {"repository": "OWNER/REPOSITORY", "grant": "GRANT"}, '
        "naming the repository your playbooks are in and, for a private one, the grant that reads it"
    ),
    command=f"sudoedit {HOST_CONFIGURATION}",
)
_START = OperatorStep(
    label="Start the worker, which names anything still missing",
    command=f"sudo {HQ_HOST} automation",
)
_LOG = OperatorStep(
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


def _absent(estate: FindingEstate) -> tuple[Finding, ...]:
    worker = estate.topology.automation_worker
    if worker is None or not worker.cannot("refresh", now=estate.now):
        return ()
    if worker.seen_at is None:
        return (
            Finding(
                rule=RULE,
                subject="",
                read_capabilities=_READ,
                title="HQ's playbook worker is not set up",
                severity="attention",
                explanation="Refresh and playbook runs are refused until it is set up on HQ's machine.",
                evidence=(("Last seen", "never"),),
                steps=(_ENTRY, _START),
            ),
        )
    return (
        Finding(
            rule=RULE,
            subject="",
            read_capabilities=_READ,
            title="HQ's playbook worker has stopped reporting",
            severity="serious",
            explanation="Refresh and playbook runs are refused until it reports again; start it on HQ's machine.",
            evidence=(("Last seen", ago(worker.seen_at, now=estate.now)),),
            steps=(_START, _LOG),
        ),
    )


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "HQ's playbook worker is not reporting",
        "attention",
        _absent,
        operator_action=(
            f"On HQ's machine, add an automation entry to {HOST_CONFIGURATION}, then run {HQ_HOST} automation as root."
        ),
        no_help_reason="HQ cannot change its machine's configuration or start its worker.",
    ),
)
