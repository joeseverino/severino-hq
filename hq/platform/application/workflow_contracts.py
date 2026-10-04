"""Dependency-free contracts shared by workflow and presentation projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionLink:
    """One safe route from observed state to an existing HQ use case."""

    name: str
    label: str
    effect: str
    url: str
    method: str = "GET"
    capability: str = ""
    target: str = ""
    reason: str = ""
    recommended: bool = False


@dataclass(frozen=True)
class WorkflowOutcome:
    """The observable fact that completes a workflow."""

    kind: str
    claim_id: str
    label: str


@dataclass(frozen=True)
class WorkflowStep:
    """One ordered phase; actions still execute through their owning use case."""

    phase: str
    label: str
    summary: str
    state: str
    actions: tuple[ActionLink, ...] = ()


@dataclass(frozen=True)
class WorkflowPlan:
    """A contextual route from evidence to action to observed resolution."""

    id: str
    label: str
    steps: tuple[WorkflowStep, ...]
    outcome: WorkflowOutcome
