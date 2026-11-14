"""The run evidence: what a piece of work shows for itself.

``{% run_evidence row.evidence %}`` draws ``partials/_run_evidence.html``
from a ``work_runs.RunEvidence``, each piece a labeled ``row-below`` block of
its row's open line: Jobs, a machine's, a procedure's and a record's page
draw a run's evidence through it. A run's own page draws the same pieces as
its facts, ``{% run_evidence run.evidence layout="facts" %}`` inside its
``{% facts %}``.
"""

from django import template

lazy from hq.platform.application import work_runs

register = template.Library()

# Each layout: a row's open line, or a run's own facts.
LAYOUTS = ("below", "facts")


@register.inclusion_tag("partials/_run_evidence.html")
def run_evidence(evidence, layout: str = "below") -> dict:
    """A run's evidence, or nothing for a row that has none."""

    if layout not in LAYOUTS:
        raise template.TemplateSyntaxError(f"run_evidence layout= is one of {', '.join(LAYOUTS)}")
    return {"evidence": evidence, "facts": layout == "facts"}


@register.filter
def job_run(job):
    """A job as the row every list of work draws (``work_runs.job_run``)."""

    return work_runs.job_run(job)


@register.filter
def standing_of(run):
    """How a run stands for a control that follows it (``work_runs.standing_of``)."""

    return work_runs.standing_of(run)
