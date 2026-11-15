"""Findings about a delivery on its way to production.

A deploy that runs, or is about to, is news rather than a fault, so it is a
notice until it stalls (``delivery_progress``). A run held for a reviewer goes
no further until somebody approves it, so it is something to do. Each is about
the delivery's own record, read as its health says (``infrastructure``).
"""

from .delivery_progress import STALLED_AFTER, WAITING, delivery_progress
from .entity_links import entity_link
from .finding_model import Finding, FindingEstate, FindingRule, opens
from .ui import counted, ended
lazy from .infrastructure import enabled_resources, resource_health

DEPLOY_WAITING = "deploy-waiting"
DEPLOYING = "deploy-running"
# How long a deploy may take before it is a problem, as a finding says it.
STALLS_AFTER = counted(int(STALLED_AFTER.total_seconds() // 60), "minute")


def _deploys(_estate: FindingEstate) -> tuple[Finding, ...]:
    found = []
    for resource in enabled_resources():
        health = resource_health(resource)
        progress = delivery_progress(resource) if health["state"] == "deploying" else None
        if progress is None:
            continue
        link = entity_link("resource", resource.key)
        said = ended(health["message"])
        if progress.state == WAITING:
            found.append(
                Finding(
                    rule=DEPLOY_WAITING,
                    subject=f"resource:{resource.key}",
                    title="A deploy is waiting for your approval",
                    severity="attention",
                    explanation=said,
                    evidence=(("Stage", "Waiting for a reviewer"),),
                    primary=opens("Approve on GitHub", progress.run_url)
                    if progress.run_url
                    else opens("Open deploy", link.url),
                    since=progress.since,
                )
            )
            continue
        found.append(
            Finding(
                rule=DEPLOYING,
                subject=f"resource:{resource.key}",
                title="A deploy is running" if progress.state == "running" else "A deploy is about to start",
                severity="attention",
                explanation=(f"{said} It becomes a problem if production is still behind after {STALLS_AFTER}."),
                evidence=(("Stage", "Running" if progress.state == "running" else "Starting"),),
                primary=opens("Open deploy", link.url),
                notice=True,
            )
        )
    return tuple(found)


def _waiting(estate: FindingEstate) -> tuple[Finding, ...]:
    return tuple(finding for finding in _deploys(estate) if finding.rule == DEPLOY_WAITING)


def _running(estate: FindingEstate) -> tuple[Finding, ...]:
    return tuple(finding for finding in _deploys(estate) if finding.rule == DEPLOYING)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        DEPLOY_WAITING,
        "Deploys awaiting approval",
        "attention",
        _waiting,
        operator_action="Approve or reject the held run on GitHub, as one of its required reviewers.",
        no_help_reason="Only a required reviewer can approve a deployment, and HQ reads GitHub with read-only tokens.",
        bars={"attention": "a deploy run is held for a reviewer, and it goes no further until you approve it"},
        clears="a reviewer approves or rejects the run",
        gathered=True,
    ),
    FindingRule(
        DEPLOYING,
        "Deploys on their way",
        "attention",
        _running,
        operator_action=f"Nothing yet: it becomes a problem if production is still behind after {STALLS_AFTER}.",
        no_help_reason="A deploy on its way is news, and it finishes on its own.",
        bars={
            "attention": "the controller reports a delivery on its way to production, which is news rather than a problem"
        },
        clears="the deploy reaches production",
        gathered=True,
    ),
)
