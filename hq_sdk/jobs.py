"""Work an extension needs to run for longer than a request will wait.

The extension owns the work and the host owns its running, so no extension
invents a thread of its own that leaves no record when the process restarts
under it. ``start(kind, label, work, requested_by=...)`` runs ``work(progress)``
and stores what it returns on the job; one job of a kind is live at a time, held
by the database, so a second start raises ``JobConflict``.
"""

from hq.domains.jobs.models import Job

# progress may be called as often as the work likes: it limits its own writes and
# is the heartbeat that says the process lives. start marks a stopped job of its
# kind lost before it records the next, and the hourly sweep marks the rest, so
# an extension has nothing to settle: a page reads Job.seen_state, since a GET
# writes nothing.
from hq.domains.jobs.runner import JobConflict, Progress, start

__all__ = ["Job", "JobConflict", "Progress", "start"]
