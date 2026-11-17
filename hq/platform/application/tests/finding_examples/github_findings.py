from datetime import timedelta

from ...inventory import record_inventory
from . import AT, Example, Examples, Raised, connection

RESETS = AT + timedelta(minutes=50)


def swept(*sweeps: tuple[int, int, int], resets=(), raises: tuple[Raised | str, ...] = ()) -> Example:
    """A GitHub App's installation swept at each (minute, used, spent): when
    the sweep started, what GitHub counted, and what the sweep itself spent."""

    def given() -> None:
        connection("example-app", "github_app")
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
)
