"""The example's outbound work, declared once.

HQ derives the job, the capability, the route, the control and the audit
entries from ``work()``; the page in ``views`` only shows what is stored.
"""

from hq_sdk.jobs import Job
from hq_sdk.outbound import Failed, OutboundWork

from . import registry

LOOKUP = "example.lookup"
NOTES = ("first-note", "second-note")
# A note the registry is never asked about, with the reason its control gives.
ARCHIVED = "archived-note"


def look_up(progress, *, subject, principal):
    progress("Asking the registry.")
    listed = registry.read(subject)
    if listed is None:
        raise Failed("The registry lists nothing for this note.")
    progress(f"The registry lists {len(listed)} entries.", force=True)
    return {"seen": len(listed), "entries": listed}


def refuse(subject):
    if subject == ARCHIVED:
        return "An archived note is not looked up."
    return "" if subject in NOTES else "No such note."


def stored(subject):
    """What the last lookup that ended well found for a note."""

    job = Job.objects.filter(
        kind=LOOKUP, state=Job.State.SUCCEEDED, request__subject=subject
    ).first()
    return job.result.get("entries", []) if job else []


def work():
    return (
        OutboundWork(
            LOOKUP,
            "Look up",
            "Ask the registry what it lists for one note.",
            "notes.write",
            look_up,
            subject_label="Note",
            subject_help="The note's slug, e.g. first-note.",
            refuse=refuse,
        ),
    )
