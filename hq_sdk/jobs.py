"""Work an extension needs to run for longer than a request will wait.

An extension owns the work; the host owns the running of it. That split is the
reason this exists at all -- without it, the first extension with something
slow to do invents a thread, the second invents a different one, and neither
records what happened when the process restarts underneath them.

    from hq_sdk.jobs import JobConflict, start

    def work(progress):
        progress("reading", percent=0)
        ...
        return {"created": 12}

    job = start("myplugin.thing", "Reading the thing", work,
                requested_by=request.user)

`progress` is safe to call as often as the work likes; it rate-limits itself
and doubles as the heartbeat that says the process is still alive. Whatever
the work returns is stored on the job and shown when it finishes.

One live job per `kind` at a time, enforced by the database rather than by
checking first -- so a double-clicked button raises `JobConflict` instead of
starting the same import twice.

`reap` closes out jobs whose process stopped without finishing. Call it from
the page that lists them: a stale job only blocks the next job of its kind, so
the moment somebody looks is the moment the answer is needed. It is exported
because an extension that starts jobs must be able to unblock them -- without
it the only way through was to import the host's `jobs` package directly,
which is precisely what this facade exists to prevent.
"""

from jobs.models import Job
from jobs.runner import JobConflict, Progress, reap, start

__all__ = ["Job", "JobConflict", "Progress", "reap", "start"]
