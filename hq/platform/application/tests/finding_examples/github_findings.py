from datetime import timedelta
from typing import Any

from hq.domains.control_plane.provider_adapters import INBOUND
from hq.platform.core.testing import connection

from ...finding_steps import Do
from ...inventory import record_inventory
from . import AT, Example, Examples, Raised

# What HQ takes from the App, by the name each is ticked under.
TAKEN = {event.partition(".")[0] for event in INBOUND["github_app"].events}

RESETS = AT + timedelta(minutes=50)


def swept(*sweeps: tuple[int, int, int], resets=(), raises: tuple[Raised | str, ...] = ()) -> Example:
    """A GitHub App's installation swept at each (minute, used, spent): when
    the sweep started, what GitHub counted, and what the sweep itself spent."""

    def given() -> None:
        connection("github_app", "example-app")
        for index, (minute, used, spent) in enumerate(sweeps):
            record = {
                "connection_ref": "example-app",
                "installation": 7,
                "account": "example",
                "limit": 5000,
                "used": used,
                "remaining": 5000 - used,
                "resets_at": (resets[index] if index < len(resets) else RESETS).isoformat(),
                "started_at": (AT - timedelta(minutes=30) + timedelta(minutes=minute)).isoformat(),
                "spent": spent,
            }
            record_inventory({"github.allowance": {"ok": True, "records": [record]}})

    return Example(given=given, raises=raises)


# A delivery the events Worker refused, as GitHub's delivery log says it.
FAILED = {
    "event": "push",
    "status": "Unauthorized",
    "status_code": 401,
    "delivered_at": (AT - timedelta(minutes=10)).isoformat(),
}


def hooked(raises: tuple[Raised | str, ...] = (), **said: Any) -> Example:
    """The App's webhook subscribed to every event HQ takes, its newest
    delivery a success, with ``said`` over that."""

    def given() -> None:
        connection("github_app", "example-app")
        record = {
            "connection_ref": "example-app",
            "app": "example-hq",
            "owner": "example",
            "events": sorted(TAKEN),
            "deliveries": 3,
            "newest": {**FAILED, "status": "Accepted", "status_code": 202},
            "failing": 0,
            **said,
        }
        record_inventory({"github.webhook": {"ok": True, "records": [record]}})

    return Example(given=given, raises=raises)


EXAMPLES = (
    Examples(
        "github-others-spent",
        fires=(
            # Past what the pipeline takes within one hour.
            swept(
                (0, 100, 40),
                (5, 140 + 700, 40),
                (10, 880 + 400, 40),
                raises=("Something besides HQ spent 1,100 GitHub requests on example",),
            ),
        ),
        quiet=(
            # Up to the usual most.
            swept((0, 100, 40), (5, 140 + 999, 40)),
            # A first sweep has nothing to judge against.
            swept((0, 3000, 40)),
            # A reset between two sweeps leaves the pair unjudged.
            swept((0, 100, 40), (60, 4000, 40), resets=(RESETS, RESETS + timedelta(hours=1))),
        ),
    ),
    Examples(
        "github-webhook",
        fires=(
            hooked(
                failing=2,
                newest=FAILED,
                raises=(
                    Raised(
                        title="2 webhook deliveries to HQ failed",
                        evidence=(
                            ("Answer", "401 Unauthorized"),
                            ("Event", "push"),
                            ("Delivered", "10\xa0minutes ago"),
                        ),
                        steps=(Do("Read the failed delivery's answer under Advanced, in Recent Deliveries."),),
                    ),
                ),
            ),
            # Subscribed to some of what HQ takes, so set up, and missing the rest.
            hooked(
                events=["push", "workflow_run", "check_suite", "pull_request", "deployment_status"],
                raises=(
                    Raised(
                        title="The GitHub App does not send deployment_review",
                        steps=(Do("Tick deployment_review under Subscribe to events, then save."),),
                    ),
                ),
            ),
        ),
        quiet=(
            hooked(newest={**FAILED, "status": "Accepted", "status_code": 202}),
            # An App never pointed at HQ is not set up.
            hooked(events=["installation"], deliveries=0, newest=None),
        ),
    ),
)
