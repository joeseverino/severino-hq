"""The link every page, answer and finding names an HQ use case by."""

from dataclasses import dataclass
from typing import Annotated, Never

from .describe import Describe
from .effects import Effect

# What `pydantic.with_config` sets, written out: each class carries its own
# config, so it is written one way under every answer that holds it.
_OWN_CONFIG = {"extra": "forbid"}


@dataclass(frozen=True, slots=True, init=False)
class ActionLink:
    """One safe route from observed state to an existing HQ use case."""

    __pydantic_config__ = _OWN_CONFIG

    def __init__(self, _factory: Never) -> None:
        raise TypeError("Build a link through read_link or command_action.")

    name: Annotated[
        str,
        Describe("What the link is for, such as `remedy` or `verify`, which a page places it by."),
    ]
    label: Annotated[str, Describe("The words on the link or button: its command's own, where it runs one.")]
    # What following it does, said once by Effect: `read` for a link that only opens a page.
    effect: Effect
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
            "Why its command would be refused now, in the command's words. When set, the link shows "
            "as unusable with it. Empty when it would run."
        ),
    ] = ""
