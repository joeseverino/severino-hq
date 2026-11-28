"""How a run ended (the bridge's ``RunOutcome``): the one record a run's result sentence is worded from (``work_runs.run_said``)."""

from enum import StrEnum


class Ending(StrEnum):
    """How a run ended, as its worker saw it or as HQ ended it without one."""

    SUCCEEDED = "succeeded"
    TASK_FAILED = "task_failed"
    UNREACHABLE = "unreachable"
    ERRORED = "errored"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    LEASE_LOST = "lease_lost"
    REFUSED = "refused"
    UNKNOWN = "unknown"


# Where an ended run's result holds how it ended (``ended_as``), and what its
# worker's report left out of its tasks and statements, in a line.
OUTCOME = "outcome"
LEFT_OUT = "left_out"


def ended_as(ending: Ending, *, task: str = "", host: str = "", message: str = "") -> dict[str, str]:
    """A ``RunOutcome``, as a worker reports one and as HQ records its own:
    the task and machine it failed on, and the ending's own words."""

    return {"ending": ending.value, "task": task, "host": host, "message": message}
