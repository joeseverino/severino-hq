"""Dependency-free contracts shared by workflow and presentation projections."""

from dataclasses import dataclass
from typing import Annotated

from .describe import Describe

# What `pydantic.with_config` sets, written here since this leaf loads no
# pydantic: each class carries its own config, so it is written one way under
# every answer that holds it.
_OWN_CONFIG = {"extra": "forbid"}


@dataclass(frozen=True, slots=True)
class ActionLink:
    """One safe route from observed state to an existing HQ use case."""

    __pydantic_config__ = _OWN_CONFIG

    name: Annotated[
        str,
        Describe("What the link is for, such as `remedy` or `verify`, which a page places it by."),
    ]
    label: Annotated[str, Describe("The words on the link or button: its command's own, where it runs one.")]
    effect: Annotated[
        str,
        Describe(
            "What following it does: `read` for a link that only opens a page, else its command's effect, "
            "`remote_write`, `infrastructure_change` or `destructive`."
        ),
    ]
    url: Annotated[str, Describe("Where it goes, a page or the command it submits to, requested with `method`.")]
    method: Annotated[str, Describe("`GET` to open `url`, `POST` to submit to it.")] = "GET"
    capability: Annotated[
        str,
        Describe("The command it runs, by its name in the command catalog; empty for a link to a page."),
    ] = ""
    target: Annotated[str, Describe("The record its command runs on; empty when the command takes none.")] = ""
    reason: Annotated[str, Describe("Why it is offered here, in a sentence; empty when that goes without saying.")] = ""
    recommended: Annotated[bool, Describe("Whether it is the press to make first.")] = False
    refusal: Annotated[
        str,
        Describe(
            "Why its command would be refused if asked for now, in the command's words: set, the link is "
            "shown unusable with it. Empty when it would run."
        ),
    ] = ""


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    """The observable fact that completes a workflow."""

    __pydantic_config__ = _OWN_CONFIG

    kind: Annotated[
        str, Describe("What completes it: `claim_absent`, once the problem it resolves is no longer reported.")
    ]
    claim_id: Annotated[
        str, Describe("The id of the problem whose absence completes it, as the problem's own `id` gives it.")
    ]
    label: Annotated[str, Describe("The outcome in words; empty when its type says it.")]


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One ordered phase; actions still execute through their owning use case."""

    __pydantic_config__ = _OWN_CONFIG

    phase: Annotated[
        str,
        Describe(
            "What the step is: `act` runs a remedy, `verify` checks it worked, `run` is an exact command the "
            "operator runs, `do` an instruction in words, `cannot` why it cannot be done from HQ."
        ),
    ]
    label: Annotated[str, Describe("The step's heading; empty for an instruction its summary says alone.")]
    summary: Annotated[
        str,
        Describe(
            "The step in full: the command of a `run` step, the instruction of a `do` step, the reason of "
            "a `cannot` step. Empty when its label says it."
        ),
    ]
    state: Annotated[
        str,
        Describe(
            "Where it stands: `recommended` to do now, `after_action` once the step before is done, "
            "`available` at any time, `operator` for a person to do by hand, `blocked` where it cannot be done."
        ),
    ]
    actions: Annotated[
        tuple[ActionLink, ...],
        Describe("The links that carry it out, each through its command's own route."),
    ] = ()


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """A contextual route from evidence to action to observed resolution."""

    __pydantic_config__ = _OWN_CONFIG

    id: Annotated[str, Describe("The plan's id, the same at every read of the problem it resolves.")]
    label: Annotated[str, Describe("The plan's heading, such as `What to do`.")]
    steps: Annotated[tuple[WorkflowStep, ...], Describe("Its steps, in the order they are taken.")]
    outcome: WorkflowOutcome
