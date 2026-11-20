"""The run evidence: what a piece of work shows for itself."""

from typing import Literal

from django import template

from hq.platform.core.part_examples import tags
from hq.platform.core.template_parts import Part, slotted
lazy from hq.platform.application import work_runs

register = template.Library()


def run_evidence(evidence, layout: Literal["below", "facts"] = "below") -> dict:
    """A run's evidence, or nothing for a row that has none."""

    return {"evidence": evidence, "facts": layout == "facts"}


slotted(
    Part(
        name="run_evidence",
        about="What a run of automation shows for itself, from a `work_runs.RunEvidence`.",
        rules="""
        What a run of automation shows for itself is `{% run_evidence evidence %}` inside a row of a
        table region. That is its machines, its correlation id, its lease while a worker holds it, and
        each task's outcome, each a labeled `row-below` block of the row's open line. Jobs draws it in a
        run's row, and a section's rows draw it through `ServiceSection.below`, so a machine's page and
        a playbook's page show a run as Jobs does. A run's own page draws the same pieces as its facts,
        `layout="facts"` inside its `{% facts %}`. A section shows it only to a reader who may open Jobs
        (`service_context.recent_work`). A section's rows end with what may be done to each record
        through `ServiceSection.actions`, drawn by `{% row_actions %}` as Jobs draws a run's Cancel
        button (`automation_runs.cancel_action`); a section none of whose rows has one draws no column
        for them.
        """,
        partial="partials/_run_evidence.html",
        function=run_evidence,
        example=tags.RUN_EVIDENCE,
    ),
    library=register,
)


@register.filter
def job_run(job):
    """A job as the row every list of work draws (``work_runs.job_run``)."""

    return work_runs.job_run(job)


@register.filter
def standing_of(run):
    """How a run stands for a control that follows it (``work_runs.standing_of``)."""

    return work_runs.standing_of(run)
