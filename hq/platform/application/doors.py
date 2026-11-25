"""How each interface a principal comes through is named: alone, and in a sentence beside who came through it,
and who did something said whole (``Attribution``)."""

from dataclasses import dataclass
from functools import cache

lazy from hq.domains.control_plane.bridge_actions import CONTROLLER
lazy from hq.domains.jobs.runner import SCHEDULER

from .labels import dotted
from .routes import reverse
from .security import (
    API_INTERFACE,
    CLI_INTERFACE,
    CONTROLLER_INTERFACE,
    DEVICE_INTERFACE,
    INTERNAL_INTERFACE,
    JOB_INTERFACE,
    MCP_INTERFACE,
    WEB_INTERFACE,
)
lazy from .tables import Cell

# Each interface named on its own, as in a list of them: "MCP, API".
LABELS = {
    WEB_INTERFACE: "web",
    DEVICE_INTERFACE: "device",
    CLI_INTERFACE: "CLI",
    MCP_INTERFACE: "MCP",
    API_INTERFACE: "API",
    JOB_INTERFACE: "job",
    CONTROLLER_INTERFACE: "controller",
    INTERNAL_INTERFACE: "internal",
}

# How each door reads beside who came through it: "example, on the web".
DOORS = {
    WEB_INTERFACE: "on the web",
    DEVICE_INTERFACE: "on their own device",
    CLI_INTERFACE: "at the shell",
    MCP_INTERFACE: "over MCP",
    API_INTERFACE: "over the API",
    JOB_INTERFACE: "in a background job",
    CONTROLLER_INTERFACE: "through the controller",
    INTERNAL_INTERFACE: "inside HQ",
}


@cache
def _machinery() -> dict[tuple[str, str], str]:
    """HQ's own machinery, by the name it acts under and its door, as it reads
    in place of both: its door would only name it again."""

    return {(CONTROLLER, CONTROLLER_INTERFACE): "HQ's controller", (SCHEDULER, JOB_INTERFACE): "HQ's schedule"}


def interface_label(interface: str) -> str:
    """An interface named on its own, as in a list of them: ``mcp`` → "MCP", ``device`` → "device".

    One HQ has not declared reads as its own name.
    """

    return LABELS.get(interface, interface)


def door_words(interface: str) -> str:
    """How ``interface`` reads beside who came through it, or nothing when no door is recorded."""

    if not interface:
        return ""
    return DOORS.get(interface, f"through {interface_label(interface)}")


def approved_words(approver: str) -> str:
    """Who agreed to something, as every page says it: "approved by joe"; nothing for nobody."""

    return f"approved by {approver}" if approver else ""


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who did something, as every page and read says it: the actor, their door or agent, and who approved it.

    Built from the record that holds the act, so one act reads alike on every
    page.
    """

    actor: str
    interface: str = ""
    through: str = ""
    approved_by: str = ""
    # The held change whose decision ``approved_by`` took, by its id, where
    # one did: its entry is where "approved by" opens.
    approval: str = ""

    def __post_init__(self) -> None:
        for name in ("through", "approved_by"):
            if getattr(self, name) == self.actor:
                object.__setattr__(self, name, "")

    def __bool__(self) -> bool:
        return bool(self.actor)

    @property
    def door(self) -> str:
        return door_words(self.interface)

    @property
    def asker(self) -> str:
        """Who acted and how: "example-agent, over MCP", or a person first and
        then the agent that acted for them, "joe, through example-agent", or
        HQ's own machinery by itself, "HQ's schedule"."""

        if self.through:
            return f"{self.actor}, through {self.through}"
        if own := _machinery().get((self.actor, self.interface)):
            return own
        return f"{self.actor}, {self.door}" if self.door else self.actor

    @property
    def approval_url(self) -> str:
        """The approval's own entry, or nothing where no held change was decided."""

        return reverse("core:approval_entry", args=[self.approval]) if self.approval and self.approved_by else ""

    @property
    def words(self) -> str:
        """The whole of it on one line: "example-agent, over MCP · approved by joe"."""

        return dotted(self.asker, self.approved)

    @property
    def approved(self) -> str:
        """Who agreed to it, in words: "approved by joe"; nothing for nobody."""

        return approved_words(self.approved_by)

    @property
    def cell(self) -> Cell:
        """``words`` as a table's cell draws it: who acted, then on its own
        line who approved it, opening the approval's entry."""

        approved = self.approved
        lines = (Cell(approved, self.approval_url),) if approved else ()
        return Cell(self.asker, cut=True, lines=lines)


# Who did something no audit row records.
NOBODY = Attribution("")
