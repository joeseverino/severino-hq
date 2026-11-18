from dataclasses import replace
from datetime import timedelta

from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.github import COMPOSE_WORKFLOW, CURRENT, KIND

from ...delivery_progress import STALLED_AFTER
from . import Example, Examples

RUN = "https://github.example.com/example/host/actions/runs/9"


def delivered(stage: str, behind_for: timedelta = timedelta(minutes=1), **extension) -> None:
    """A delivery whose approved commit production does not run yet, at ``stage`` as the controller words it."""

    ManagedResource.objects.create(
        key="delivery",
        kind=KIND,
        spec={"repository": "example/host", "workflow": COMPOSE_WORKFLOW, "branch": "main", "production": CURRENT},
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


def delivery(stage: str, **said) -> Example:
    return Example(readings={KIND: ()}, given=lambda: delivered(stage, **said))


def raising(example: Example, *titles: str) -> Example:
    return replace(example, raises=titles)


RUNNING = delivery("Compose run 9 is running")
WAITING = delivery("Deploy run 9 is waiting for approval")
# A run that failed, and one on its way for longer than a deploy takes, are problems rather than news.
ENDED = (
    delivery("Compose run 9 failed"),
    delivery("Compose run 9 is running", behind_for=STALLED_AFTER + timedelta(minutes=1)),
)

EXAMPLES = (
    Examples("deploy-waiting", fires=(raising(WAITING, "A deploy is waiting for your approval"),), quiet=(RUNNING,)),
    Examples(
        "deploy-running",
        fires=(
            raising(RUNNING, "A deploy is running"),
            raising(delivery("No deploy has started"), "A deploy is about to start"),
            # The stage the reading states is read before its sentence.
            raising(delivery("Compose run 9 failed", stage_state="running"), "A deploy is running"),
        ),
        quiet=(WAITING, *ENDED),
    ),
)
