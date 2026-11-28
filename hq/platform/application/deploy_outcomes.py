"""How a deployment GitHub records ended, in words and a tone: the outcome its
deploy reported where deploy/outcomes.json declares one, else its state."""

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings

from .tone import Tone

OUTCOMES = Path(settings.BASE_DIR) / "deploy" / "outcomes.json"
# A state no declared outcome names, as what happened to the deployment.
_STATES = {
    "success": ("Deployed", Tone.GOOD),
    "failure": ("Deploy failed", Tone.SERIOUS),
    "error": ("Deploy failed", Tone.SERIOUS),
    "inactive": ("Replaced", Tone.ATTENTION),
}
_GOING = ("Deploying", Tone.ATTENTION)


@cache
def declared() -> Mapping[str, Mapping[str, Any]]:
    """Each outcome deploy/outcomes.json declares, by its name."""

    return json.loads(OUTCOMES.read_text(encoding="utf-8"))["outcomes"]


def outcome(deployment: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The declared outcome its deploy reported, or None."""

    return declared().get(str(deployment.get("outcome") or ""))


def went_live(deployment: Mapping[str, Any]) -> bool:
    """Whether production ran it once its deploy ended: its declared outcome
    says so, or, for a deploy that declares none, its state."""

    declared_outcome = outcome(deployment)
    if declared_outcome is not None:
        return declared_outcome["production"] == "release"
    return deployment.get("state") == "success"


def said(deployment: Mapping[str, Any]) -> tuple[str, Tone]:
    """How it ended, in a few words, and in which tone."""

    declared_outcome = outcome(deployment)
    if declared_outcome is None:
        return _STATES.get(str(deployment.get("state") or ""), _GOING)
    if declared_outcome["production"] == "release":
        return ("Live, a check failed", Tone.ATTENTION) if declared_outcome["check_failed"] else ("Live", Tone.GOOD)
    if declared_outcome["production"] == "previous":
        return "Not deployed", Tone.SERIOUS if declared_outcome["check_failed"] else Tone.NEUTRAL
    return "Production unknown", Tone.SERIOUS
