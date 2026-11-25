"""The shapes the dashboard's reads answer in: the estate's figures, the action
items and HQ's own problems."""

from typing import Annotated, TypedDict

from .answer_fields import Since
from .describe import Describe, listed
from .finding_model import FindingEvidence
from .finding_steps import WrittenCheck, WrittenReason, WrittenSteps
from .plugins import ATTENTION_ORDER
from .ui import ended
from .workflows import WrittenAction


class EstateFigure(TypedDict):
    id: Annotated[str, Describe("The figure's stable id, such as `hq.estate.machines`.")]
    label: Annotated[str, Describe("What it counts, as the dashboard heads it.")]
    value: Annotated[str, Describe("The figure as a page shows it, such as `3` or `24 days`.")]
    detail: Annotated[str, Describe("A line under it, such as how many are offline; empty for none.")]
    status: Annotated[str, Describe("`attention` when it needs a look; empty otherwise.")]
    url: Annotated[str, Describe("The page that lists what it counts, or of the one thing it is about.")]


class ActionItemRow(TypedDict):
    status: Annotated[str, Describe(ended(f"How urgent it is: {listed(ATTENTION_ORDER)}"))]
    source: Annotated[str, Describe("The part of HQ it comes from, in words, such as `Containers`.")]
    label: Annotated[str, Describe("What needs doing, in a line.")]
    detail: Annotated[str, Describe("More on it: what was read, and what to do.")]
    action: Annotated[
        str,
        Describe(
            "What it leads with: the button that opens it, such as `Open containers`, else the first command HQ runs for it; empty for neither."
        ),
    ]
    url: Annotated[str, Describe("The page where it is dealt with.")]
    since: Since


class ActionItem(ActionItemRow):
    """One action item: what needs doing, where, and the calls and commands that do it."""

    key: Annotated[str, Describe("The item's key, which setting it aside names it by.")]
    source_id: Annotated[str, Describe("The stable id of the part of HQ it comes from, such as `containers`.")]
    count: Annotated[int, Describe("How many things it stands for, at least 1.")]
    steps: WrittenSteps
    no_help_reason: WrittenReason
    check: WrittenCheck
    actions: Annotated[
        list[WrittenAction], Describe("The quick actions offered on it, each through its owner's own command or page.")
    ]
    subject: Annotated[
        WrittenAction | None, Describe("The thing it is about, as a link to its own page; null for none.")
    ]
    notice: Annotated[bool, Describe("Whether it only reports something, with nothing to resolve.")]
    observed_at: Annotated[str, Describe("When the reading it is drawn from was taken, in ISO 8601; empty for none.")]
    facts: Annotated[list[FindingEvidence], Describe("What was read that shows it, a labeled line each.")]
    links: Annotated[
        list[WrittenAction], Describe("Pages that help look into it, such as where it sits; never one that acts.")
    ]


class Problem(TypedDict):
    """One open problem about HQ itself."""

    key: Annotated[
        str, Describe("What the problem is about, stable while it is the same matter, such as `approval:<id>`.")
    ]
    label: Annotated[str, Describe("What is wrong with HQ, in a line.")]
    detail: Annotated[str, Describe("More on the problem: what was read, and what to do.")]
    status: Annotated[str, Describe(ended(f"How serious the problem is: {listed(ATTENTION_ORDER)}"))]
    url: Annotated[str, Describe("Where the problem is dealt with in HQ.")]
    subject: WrittenAction | None
    since: Since
