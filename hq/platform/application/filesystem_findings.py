"""Findings about a filesystem on a machine that is running out of space.

A full disk fails whatever writes next (a build, a backup, the database)
with an error that names the write, not the disk, so this reads the
``host.filesystem`` reading's one row per filesystem (docs/Derived-Facts.md
says how it is taken).
"""

import shlex
from dataclasses import dataclass
from typing import Any

from hq.domains.control_plane.observations.host import FILESYSTEM_KIND

from .finding_model import FindingRule, cannot_run_commands, machine_step
from .finding_steps import Run
from .labels import human_bytes
from .reading_facts import OnEachMachine, ReadingRow
from .thresholds import Threshold
from .tone import Tone
from .topology_model import TopologyNode
from .ui import counted, measured

GIB = 1 << 30
MIB = 1 << 20

# A filesystem is nearly full when its free space is under both a floor and a
# share of its size. A share alone flags a large disk that still has plenty
# left; a floor alone flags every small partition (a 1 GiB /boot) for good.
ATTENTION_FREE = Threshold(2 * GIB, "bytes")
ATTENTION_SHARE = Threshold(0.10, "share")
SERIOUS_FREE = Threshold(512 * MIB, "bytes")
SERIOUS_SHARE = Threshold(0.03, "share")


@dataclass(frozen=True, slots=True)
class FilesystemSpace(ReadingRow):
    """One filesystem as its record says it."""

    KIND = FILESYSTEM_KIND

    mount: str = ""
    free_bytes: str = ""
    total_bytes: str = ""
    read_at: str = ""

    @classmethod
    def record(cls, record: dict[str, Any], snapshot: Any) -> FilesystemSpace:
        return filesystem_space(record)

    @property
    def free(self) -> int:
        return int(self.free_bytes or 0)

    @property
    def total(self) -> int:
        return int(self.total_bytes or 0)

    def under(self, floor: int, share: float) -> bool:
        return self.total > 0 and self.free < floor and self.free < self.total * share

    @property
    def severity(self) -> str:
        """``serious`` or ``attention`` when nearly full, else ""."""

        if self.under(SERIOUS_FREE.value, SERIOUS_SHARE.value):
            return Tone.SERIOUS
        if self.under(ATTENTION_FREE.value, ATTENTION_SHARE.value):
            return Tone.ATTENTION
        return ""


def filesystem_space(record: dict[str, Any]) -> FilesystemSpace:
    """One ``host.filesystem`` record as the rule reads it."""

    return FilesystemSpace(
        mount=str(record.get("mount", "") or ""),
        free_bytes=str(record.get("free_bytes", 0) or 0),
        total_bytes=str(record.get("total_bytes", 0) or 0),
        read_at=str(record.get("read_at", "") or ""),
    )


def _share(filesystem: FilesystemSpace) -> str:
    return measured(filesystem.free * 100 / filesystem.total, "%", 1)


def _steps(node: TopologyNode, row: FilesystemSpace) -> tuple[Run, ...]:
    mount = shlex.quote(row.mount)
    return (
        *machine_step(f"See the space on {row.mount}", node.label, f"df -h {mount}"),
        *machine_step(
            f"Find what fills {row.mount}",
            node.label,
            f"du -xh --max-depth=1 {mount} | sort -h | tail",
            ("Free space there, then check again.",),
        ),
    )


# A filesystem whose free space, when it was read, was under both the floor
# and the share of its size: one finding per machine, at the worst
# filesystem's severity.
_nearly_full = OnEachMachine(
    rule="filesystem-nearly-full",
    row=FilesystemSpace,
    picked=lambda row: bool(row.mount and row.severity),
    severity=lambda full: Tone.SERIOUS if any(row.severity == Tone.SERIOUS for row in full) else Tone.ATTENTION,
    title=lambda node, full: (
        f"The filesystem at {full[0].mount} on {node.label} is nearly full"
        if len(full) == 1
        else f"{counted(len(full), 'filesystem is', 'filesystems are')} nearly full on {node.label}"
    ),
    explanation=lambda _full: "Writes fail once it fills.",
    # The mount stands in the label, so no value reads as a link's path.
    evidence=lambda row: (
        (f"Free on {row.mount}", f"{human_bytes(row.free)} of {human_bytes(row.total)} ({_share(row)})"),
    ),
    steps=_steps,
)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "filesystem-nearly-full",
        "Filesystem nearly full",
        Tone.SERIOUS,
        _nearly_full,
        gathered=True,
        operator_action="Free space on the mount, then check again.",
        no_help_reason=cannot_run_commands(),
        bars={
            Tone.SERIOUS: f"free space is under {SERIOUS_FREE} and {SERIOUS_SHARE} of the disk",
            Tone.ATTENTION: f"free space is under {ATTENTION_FREE} and {ATTENTION_SHARE} of the disk",
        },
        clears=f"free space is back above {ATTENTION_FREE} or {ATTENTION_SHARE} of the disk",
    ),
)
