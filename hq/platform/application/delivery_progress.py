"""Whether a delivery that is behind production is still on its way there.

The controller reports a delivery as having a problem from the moment a newer
commit is approved until production runs it. Most of that time nothing is
wrong: the deploy has not started yet, or it is running. That is a notice. It
is a problem once a run has failed or ended without deploying, or once the
delivery has been behind for longer than ``STALLED_AFTER``. A run waiting for
approval waits on the owner, which is neither.

Each plugin's stage is read from ``stage_state`` where the reading carries it.
A reading without it carries only the controller's sentence, whose closing
words are fixed (controller/providers/github_delivery.go).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from hq.domains.control_plane.provider_adapters.github import KIND as DELIVERY_KIND

from .conditions import held_since
from .derivations import reached
from .entity_links import web_url

# How long a delivery may be behind production before that is a problem.
STALLED_AFTER = timedelta(minutes=30)

STARTING = "not_started"
RUNNING = "running"
WAITING = "waiting"
ENDED = "ended"
_ON_ITS_WAY = (STARTING, RUNNING)
# The controller's closing words for each stage that is not an end.
_SENTENCES = (
    ("No deploy has started", STARTING),
    ("is running", RUNNING),
    ("is waiting for approval", WAITING),
)


@dataclass(frozen=True)
class Progress:
    """Where a delivery behind production stands, at its least advanced plugin."""

    state: str
    since: datetime | None
    run_url: str = ""

    @property
    def stalled_at(self) -> datetime | None:
        return self.since + STALLED_AFTER if self.since else None


def _stage(item: Mapping[str, Any]) -> str:
    stated = str(item.get("stage_state", "") or "")
    if stated:
        return stated if stated in (STARTING, RUNNING, WAITING) else ENDED
    said = str(item.get("stage", "") or "").strip().rstrip(".")
    return next((state for words, state in _SENTENCES if said.endswith(words)), ENDED)


def delivery_progress(resource) -> Progress | None:
    """How a delivery that reports a problem is getting on; None when it is
    not a delivery, is not behind, or a run has ended without deploying."""

    if resource.kind != DELIVERY_KIND:
        return None
    behind = [
        item
        for item in (resource.status or {}).get("extensions") or ()
        if isinstance(item, Mapping)
        and item.get("admitted")
        and item.get("admitted") != item.get("running")
    ]
    stages = {_stage(item) for item in behind}
    if not stages or ENDED in stages:
        return None
    state = WAITING if WAITING in stages else RUNNING if RUNNING in stages else STARTING
    run_url = next((web_url(item.get("run_url")) for item in behind if _stage(item) == state), "")
    return Progress(state, held_since(resource.conditions, "Degraded"), run_url)


def on_its_way(progress: Progress | None) -> bool:
    """Whether a deploy is merely in progress: started or about to, and not
    behind for longer than ``STALLED_AFTER``. When it began to be behind is not
    known for a delivery first read that way, and then it is not called stalled."""

    if progress is None or progress.state not in _ON_ITS_WAY:
        return False
    return progress.stalled_at is None or not reached(progress.stalled_at)


LABELS = {
    STARTING: "Deploy about to start",
    RUNNING: "Deploy running",
    WAITING: "Deploy waiting for approval",
}


def deploying_label(resource) -> str:
    """What a delivery that is not at fault is doing, as its state says it;
    "" for anything else, a failed run and a stalled one included."""

    progress = delivery_progress(resource)
    if progress is None or not (progress.state == WAITING or on_its_way(progress)):
        return ""
    return LABELS[progress.state]
