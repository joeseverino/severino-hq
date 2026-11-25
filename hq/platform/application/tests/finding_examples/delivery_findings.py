from dataclasses import replace
from datetime import timedelta

from django.utils import timezone

from hq.domains.control_plane.provider_adapters.github import COMPOSE_WORKFLOW, CURRENT, KIND
from hq.platform.core.testing import declare

from ...delivery_progress import STALLED_AFTER
from . import Example, Examples, Raised

RUN = "https://github.example.com/example/host/actions/runs/9"


def delivered(stage: str, state: str, behind_for: timedelta = timedelta(minutes=1), **extension) -> None:
    """A delivery whose approved commit production does not run yet, at ``stage`` as the controller words
    it and ``state`` as it decides it."""

    declare(
        "delivery",
        KIND,
        {"repository": "example/host", "workflow": COMPOSE_WORKFLOW, "branch": "main", "production": CURRENT},
        generation=1,
        observed_generation=1,
        last_observed_at=timezone.now(),
        status={
            "extensions": [
                {
                    "plugin": "example.alpha",
                    "running": "a" * 40,
                    "admitted": "b" * 40,
                    "stage": stage,
                    "stage_state": state,
                    "run_url": RUN,
                    **extension,
                }
            ]
        },
        conditions=[
            {
                "type": "Degraded",
                "status": True,
                "reason": "NotDelivered",
                "message": f"example.alpha: bbbbbbb is approved, production still runs aaaaaaa. {stage}.",
                "since": (timezone.now() - behind_for).isoformat(),
            }
        ],
    )


def delivery(stage: str, state: str, **said) -> Example:
    return Example(readings={KIND: ()}, given=lambda: delivered(stage, state, **said))


def raising(example: Example, *raised: str | Raised) -> Example:
    return replace(example, raises=raised)


def on_its_way(title: str) -> Raised:
    """A deploy's notice, told of by the commit on its way."""

    return Raised(title=title, notice="b" * 40)


RUNNING = delivery("Compose run 9 is running", "running")
WAITING = delivery("Deploy run 9 is waiting for approval", "waiting")
# A run that failed, and one on its way for longer than a deploy takes, are problems rather than news.
ENDED = (
    delivery("Compose run 9 failed", "failed"),
    delivery("Compose run 9 is running", "running", behind_for=STALLED_AFTER + timedelta(minutes=1)),
)

EXAMPLES = (
    Examples("deploy-waiting", fires=(raising(WAITING, "A deploy is waiting for your approval"),), quiet=(RUNNING,)),
    Examples(
        "deploy-running",
        fires=(
            raising(RUNNING, on_its_way("A deploy is running")),
            raising(delivery("No deploy has started", "not_started"), on_its_way("A deploy is about to start")),
        ),
        quiet=(WAITING, *ENDED),
    ),
)
