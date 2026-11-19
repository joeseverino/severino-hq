"""The parts several kinds' records answer in alike: a link, a source, a check.

Each kind's row, item and record is a TypedDict in its own ``<kind>_records``
module. The API document types each read by them, and a test holds every
answer on the showcase data to its type. A part in its source's own shape is
``AsRead``.
"""

from typing import Annotated, Any, TypedDict

from .describe import Describe, listed
from .entity_links import EntityLink
from .json_schemas import Written
from .path_model import HopSource
from .standards import INTENDED, Standing
from .tone import Tone
from .ui import ended

# What a container row and a machine's list of its containers say alike.
type ComposeProject = Annotated[str, Describe("The compose project it belongs to; empty for one started on its own.")]


# The state a line of a list or a summary may carry, as its pill is toned.
type OptionalTone = Annotated[str, Describe(f"How it stands: {listed(Tone)}, or empty for no state.")]

# A name and the page it opens, as ``EntityLink`` says it.
WrittenLink = Annotated[dict[str, Any], Written(EntityLink)]


# Where HQ read a hop, as ``HopSource`` says it.
WrittenSource = Annotated[dict[str, Any], Written(HopSource)]


class CheckPart(TypedDict):
    id: Annotated[str, Describe("The check's stable id, such as `edge` or `tailnet-lock`.")]
    label: Annotated[str, Describe("What is true when it is met, in words.")]
    state: Annotated[
        str,
        Describe(ended(f"Where it stands: {listed((*Standing, INTENDED))}; the last is failed on purpose")),
    ]
    limited: Annotated[bool, Describe("Met, with a limit the reader should see.")]
    serious: Annotated[bool, Describe("Whether failing it lets someone or something gain reach.")]
    detail: Annotated[str, Describe("Why it was decided so, or why it is failed on purpose, in a sentence.")]
    evidence: Annotated[str, Describe("What it was decided from, or why HQ could not read it; empty for none.")]
    why: Annotated[str, Describe("Why it matters, in a sentence.")]
    fix: Annotated[str, Describe("How it is met, in a sentence.")]
    read_with: Annotated[str, Describe("What would let HQ read a check it could not; empty for none.")]
    read_with_url: Annotated[str, Describe("Where that is done; empty for none.")]
    boundary: Annotated[str, Describe("The security boundary it belongs to; empty for none.")]
    mechanism: Annotated[str, Describe("What enforces it; empty for none.")]
    rules: Annotated[list[str], Describe("The rules behind it, where what decides it has rules.")]


class MeasurementPart(TypedDict):
    """One subject against a standard: how many checks it meets, and each check."""

    met: Annotated[int, Describe("How many checks are met.")]
    measured: Annotated[int, Describe("How many checks HQ could read.")]
    unread: Annotated[int, Describe("How many checks HQ could not read.")]
    checks: Annotated[list[CheckPart], Describe("Each check that applies, in the standard's order.")]
