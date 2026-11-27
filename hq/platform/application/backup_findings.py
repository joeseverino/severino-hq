"""HQ's off-site backup copy, from the ``r2.backup`` reading of each backup
connection, and the finding that it is late: older than the backup unit's
own schedule allows, or none yet while a connection has long been set up.

The controller copies each nightly archive on its first applying pass after
the backup unit's run (docs/Backup.md, The off-site copy), so a copy is late
once two of that unit's runs, each up to its randomized delay late, have
gone by with none newer.
"""

from dataclasses import dataclass
from datetime import datetime

from hq.domains.control_plane.observations.r2 import BACKUP_KIND

from . import unit_timers
from .finding_model import Finding, FindingEstate, FindingRule
from .finding_steps import Do, Run
from .freshness import missed
from .moments import duration, moment, when
from .projection import read_once
from .tone import Tone
from .ui import counted
lazy from .connections import connection_rows
lazy from .derivations import passed
lazy from .entity_links import entity_link
lazy from .facts import stored_snapshots
lazy from .unit_findings import unit_causes

RULE = "offsite-backup-late"
PROVIDER = "r2"
BACKUP_UNIT = "severino-hq-backup.service"
BACKUP_TIMER = "severino-hq-backup.timer"
# How old the newest off-site copy may be: two of the backup unit's runs.
COPIED_WITHIN = missed(
    unit_timers.every(BACKUP_TIMER) + unit_timers.span(BACKUP_TIMER, "RandomizedDelaySec"), 2
).stale_after
_ALLOWED = duration(COPIED_WITHIN)


@dataclass(frozen=True, slots=True)
class OffSiteCopy:
    """The newest backup archive one connection's bucket holds, as its
    reading says; no archive while the bucket holds none, or was never read."""

    connection_ref: str
    # When a controller first reported the connection.
    set_up_at: datetime
    # Whether the connection's item lets the controller upload.
    manages: bool
    key: str = ""
    uploaded_at: datetime | None = None
    copies: int = 0
    # When the bucket was last read with an answer.
    read_at: datetime | None = None
    # Whether the last read of the bucket answered, so no archive is known
    # to mean the bucket holds none.
    answered: bool = False

    @property
    def url(self) -> str:
        """The connection's page."""

        return entity_link("connection", self.connection_ref).url

    def late(self, now: datetime) -> bool:
        """Whether the newest copy is older than the backup's schedule
        allows, or none has arrived in that long since it was set up."""

        if self.uploaded_at is None and not self.answered:
            return False
        return passed((self.uploaded_at or self.set_up_at) + COPIED_WITHIN, now=now)


def off_site_copies() -> tuple[OffSiteCopy, ...]:
    """Each backup connection's newest off-site copy, by connection."""

    return read_once("backup_findings.off_site", _copies)


def _copies() -> tuple[OffSiteCopy, ...]:
    snapshot = next(iter(stored_snapshots().get(BACKUP_KIND, ())), None)
    records = {str(record.get("connection_ref", "")): record for record in (snapshot.records if snapshot else ())}
    found: dict[str, OffSiteCopy] = {}
    for row in connection_rows():
        if row.provider != PROVIDER or row.connection_ref in found:
            continue
        record = records.get(row.connection_ref, {})
        found[row.connection_ref] = OffSiteCopy(
            connection_ref=row.connection_ref,
            set_up_at=row.created_at,
            manages=row.manages,
            key=str(record.get("key", "")),
            uploaded_at=moment(record.get("uploaded_at")),
            copies=int(record.get("copies", 0) or 0),
            read_at=snapshot.observed_at if snapshot else None,
            answered=snapshot is not None and snapshot.connected and snapshot.reachable,
        )
    return tuple(sorted(found.values(), key=lambda copy: copy.connection_ref))


def _late(estate: FindingEstate) -> tuple[Finding, ...]:
    nodes = {node.connection_ref: node for node in estate.nodes() if node.kind == "connection" and node.connection_ref}
    found = []
    for copy in off_site_copies():
        if not copy.late(estate.now):
            continue
        node = nodes.get(copy.connection_ref)
        label = node.label if node is not None else copy.connection_ref
        found.append(
            Finding(
                rule=RULE,
                subject=node.id if node is not None else "",
                part="" if node is not None else copy.connection_ref,
                title=f"{label}: off-site backup is late" if copy.uploaded_at else f"{label}: no off-site backup yet",
                severity=Tone.ATTENTION,
                explanation="Losing HQ's machine would lose the backups no off-site copy holds.",
                evidence=(
                    ("Newest copy", when(copy.uploaded_at, now=estate.now) if copy.uploaded_at else "none"),
                    ("Allowed age", _ALLOWED),
                    *((("Copies kept", counted(copy.copies, "copy", "copies")),) if copy.copies else ()),
                ),
                steps=(
                    (
                        Run(
                            label="Read the backup's last runs",
                            command=f"sudo journalctl -u {BACKUP_UNIT} -n 50 --no-pager",
                        ),
                    )
                    if copy.manages
                    else (Do("Set manages to 1 on the connection's item, so the controller uploads each archive"),)
                ),
                observed_at=copy.read_at,
                reads=BACKUP_KIND,
                follows=unit_causes(estate, BACKUP_UNIT),
            )
        )
    return tuple(found)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "Off-site backup late",
        Tone.ATTENTION,
        _late,
        gathered=True,
        operator_action="Check the backup unit ran, then that the controller's next pass copies its archive.",
        no_help_reason="HQ cannot run its own machine's backup.",
        bars={Tone.ATTENTION: f"the newest off-site backup is older than {_ALLOWED}"},
        clears=f"a copy newer than {_ALLOWED} arrives",
    ),
)
