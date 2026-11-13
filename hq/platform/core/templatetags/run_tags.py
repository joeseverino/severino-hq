"""The run evidence: what a run of automation shows for itself, in its row's
open line.

``{% run_evidence row.evidence %}`` draws ``partials/_run_evidence.html``
from a ``work_runs.RunEvidence``: its machines, who approved it, its
correlation id, its lease while a worker holds it, and each task's outcome,
each a labeled ``row-below`` block. Jobs, a machine's page and a procedure's
page draw a run's evidence through it.
"""

from django import template

register = template.Library()


@register.inclusion_tag("partials/_run_evidence.html")
def run_evidence(evidence) -> dict:
    """A run's evidence, or nothing for a row that has none."""

    return {"evidence": evidence}
