"""Work too slow for a request, and honest about where it has got to.

Reading a multi-gigabyte archive takes minutes and cannot be uploaded, so the
page that starts it has to return before the work finishes. The alternative is
a request that hangs until the proxy gives up, leaving no way to tell whether
the work finished or died.

So a job is a row: created before the work starts, updated while it runs, and
outliving it either way. That buys three things: a page that can ask "and
now?" cheaply, evidence when a process is killed mid-run, and a lock that
stops the same job starting twice.

`kind` is a string the caller chooses and this app never interprets, so an
extension owns the work while the host owns the running of it.
"""

import uuid
from datetime import timedelta
from typing import override

from django.conf import settings
from django.db import models
from django.utils import timezone

from hq.platform.core.rules import Rule, one_of

# How long a running job may go without a heartbeat before it is presumed
# dead. Generous: a slow step that reports nothing for two minutes is working,
# and declaring it dead is worse than waiting.
HEARTBEAT_GRACE = timedelta(minutes=5)


class JobState(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    # Distinct from FAILED: a vanished process is a restart to retry,
    # a raised exception is a traceback to read.
    LOST = "lost", "Interrupted"


class Job(models.Model):
    """One piece of work that outlives the request that asked for it."""

    # Declared above the model so a rule in ``Meta`` can name its values.
    State = JobState

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Namespaced by convention ("<extension>.<work>") so the list stays
    # readable with several extensions running work.
    kind = models.CharField(max_length=64, db_index=True)
    label = models.CharField(max_length=200)
    state = models.CharField(
        max_length=16, choices=State, default=State.QUEUED, db_index=True
    )
    # 0 to 100 (``job_percent_at_most_100``), or null where the work cannot
    # say: better than a number that stops moving.
    percent = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    actor = models.CharField(max_length=160, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
    )
    # What the caller passed in and what the work produced. Both opaque here.
    request = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Touched by every progress report. Not `auto_now`, which any unrelated
    # write would also move: this has to stop when the process stops.
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # One live job per kind, enforced by the database rather than by
            # checking first, which lets two through on a double-click.
            models.UniqueConstraint(
                fields=("kind",),
                condition=models.Q(state__in=("queued", "running")),
                name="one_live_job_per_kind",
            ),
            Rule(
                condition=models.Q(percent__isnull=True) | models.Q(percent__lte=100),
                name="job_percent_at_most_100",
                violation_error_message="Progress is 0 to 100.",
                field="percent",
            ),
            one_of("state", JobState, "job_state_is_declared"),
        ]

    @override
    def __str__(self):
        return f"{self.kind}: {self.get_state_display()}"

    @property
    def is_live(self) -> bool:
        return self.state in {self.State.QUEUED, self.State.RUNNING}

    @property
    def is_stale(self) -> bool:
        """Running, but nothing has been heard from it for too long."""
        if self.state != self.State.RUNNING or self.heartbeat_at is None:
            return False
        return timezone.now() - self.heartbeat_at > HEARTBEAT_GRACE

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or self.heartbeat_at or timezone.now()
        return (end - self.started_at).total_seconds()
