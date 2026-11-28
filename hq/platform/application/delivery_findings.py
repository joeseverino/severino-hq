"""Findings about a delivery on its way to production.

A deploy that runs, or is about to, is news rather than a fault, so it is a
notice until it stalls (``delivery_progress``). A run held for a reviewer is
the repository's ``github-waiting``, which names the run and how long it has
waited. Each is about the delivery's own record, read as its health says
(``infrastructure``).
"""

from .delivery_progress import STALLED_AFTER, WAITING, delivery_progress
from .entity_links import entity_link
from .finding_model import Finding, FindingEstate, FindingRule, Notice, opens
from .moments import Span
from .references import Kind
from .tone import Tone
from .ui import ended
lazy from .infrastructure import enabled_resources, resource_health

DEPLOYING = "deploy-running"
# How long a deploy may take before it is a problem, as a finding says it.
STALLS_AFTER = Span(STALLED_AFTER)


def _deploys(_estate: FindingEstate) -> tuple[Finding, ...]:
    found = []
    for resource in enabled_resources():
        health = resource_health(resource)
        progress = delivery_progress(resource) if health["state"] == "deploying" else None
        if progress is None or progress.state == WAITING:
            continue
        link = entity_link("resource", resource.key)
        found.append(
            Finding(
                rule=DEPLOYING,
                subject=Kind.RESOURCE.of(resource.key),
                title="A deploy is running" if progress.state == "running" else "A deploy is about to start",
                severity=Tone.ATTENTION,
                explanation=ended(health["message"]),
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


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        DEPLOYING,
        "Deploys on their way",
        Tone.ATTENTION,
        _deploys,
        operator_action=f"Nothing, unless it runs past {STALLS_AFTER}.",
        no_help_reason="It finishes on its own.",
        bars={Tone.ATTENTION: "a deploy is on its way to production"},
        clears="the deploy reaches production",
        gathered=True,
    ),
)
