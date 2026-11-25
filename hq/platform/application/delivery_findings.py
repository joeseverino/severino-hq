"""Findings about a delivery on its way to production.

A deploy that runs, or is about to, is news rather than a fault, so it is a
notice until it stalls (``delivery_progress``). A run held for a reviewer goes
no further until somebody approves it, so it is something to do. Each is about
the delivery's own record, read as its health says (``infrastructure``).
"""

from .delivery_progress import STALLED_AFTER, WAITING, delivery_progress
from .entity_links import entity_link
from .finding_model import Finding, FindingEstate, FindingRule, Notice, opens
from .references import Kind
from .tone import Tone
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
                    subject=Kind.RESOURCE.of(resource.key),
                    title="A deploy is waiting for your approval",
                    severity=Tone.ATTENTION,
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
                subject=Kind.RESOURCE.of(resource.key),
                title="A deploy is running" if progress.state == "running" else "A deploy is about to start",
                severity=Tone.ATTENTION,
                explanation=said,
                evidence=(
                    ("Stage", "Running" if progress.state == "running" else "Starting"),
                    ("Stalls after", STALLS_AFTER),
                ),
                primary=opens("Open deploy", link.url),
                # Dismissed, it stays gone until another commit is on its way.
                notice=Notice(progress.admitted),
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
        Tone.ATTENTION,
        _waiting,
        operator_action="Approve or reject the run on GitHub.",
        no_help_reason="HQ's GitHub access is read-only.",
        bars={Tone.ATTENTION: "a deploy run is held for a reviewer"},
        clears="a reviewer approves or rejects the run",
        gathered=True,
    ),
    FindingRule(
        DEPLOYING,
        "Deploys on their way",
        Tone.ATTENTION,
        _running,
        operator_action=f"Nothing, unless it runs past {STALLS_AFTER}.",
        no_help_reason="It finishes on its own.",
        bars={Tone.ATTENTION: "a deploy is on its way to production"},
        clears="the deploy reaches production",
        gathered=True,
    ),
)
