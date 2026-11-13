"""Readings a controller takes of the machine it runs on and the edges it can see."""

import re
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, ConfigDict, Field

from .contract import ObservationRecord, ObservationSpec, Stamp


class HostFirewallRecord(ObservationRecord):
    record: str
    interface: str = ""
    accept_requires_interface: bool = False
    foreign_interface_dropped: bool = False
    read_at: Stamp = None


class HostTailnetLockRecord(ObservationRecord):
    """Tailnet lock as the controller's own machine sees it: `tailscale lock
    status`, counted. It names no key, only how many sign."""

    record: str
    enabled: bool = False
    node_key_signed: bool = False
    trusted_keys: int = Field(default=0, ge=0)
    # The machines lock filters out of the tailnet, by name.
    locked_out: tuple[str, ...] = ()


class HostPerimeterRecord(ObservationRecord):
    record: str
    connection_ref: str
    firewall_unit: str = "unknown"
    public_addresses: tuple[str, ...] = ()
    ports_checked: tuple[int, ...] = ()
    answered_publicly: tuple[int, ...] = ()
    read_at: Stamp = None


# The only strings a secret renderer's status document carries besides its
# times: one short word each, as the renderer restricts them when it writes
# (``WordPattern`` in controller/secretstatus). A string that is not one is
# stored as ``UNREADABLE_WORD``, never as what it said.
STATUS_WORD = r"^[A-Za-z0-9_.+-]{1,64}$"
UNREADABLE_WORD = "unreadable"
# A renderer's name is a fixed word the launcher chooses, never a path.
RENDERER_NAME = r"^[a-z][a-z0-9-]{0,31}$"
# What a record says of the read itself, and why a document could not be read.
RENDER_READ = "read"
RENDER_UNREADABLE = "unreadable"
RENDER_REASONS = ("missing", "unreadable", "oversized", "invalid")

_STATUS_WORD = re.compile(STATUS_WORD)


def _word(value: Any) -> str:
    return value if isinstance(value, str) and _STATUS_WORD.fullmatch(value) else UNREADABLE_WORD


Word = Annotated[str, BeforeValidator(_word)]


class SecretRenderAttempt(ObservationRecord):
    at: Stamp = None
    outcome: Word = UNREADABLE_WORD
    failure: Word = ""


class SecretRenderCounts(ObservationRecord):
    items_read: int = 0
    connections: int = 0
    app_variables: int = 0
    identities: int = 0
    signing_keys: int = 0


class SecretRenderSuccess(ObservationRecord):
    at: Stamp = None
    rendered_at: Stamp = None
    content_version: int | None = None
    attribute_version: int | None = None
    counts: SecretRenderCounts = SecretRenderCounts()


class SecretRenderDependency(ObservationRecord):
    service: Word
    status: Word


class SecretRenderConnect(ObservationRecord):
    read_at: Stamp = None
    version: Word = UNREADABLE_WORD
    dependencies: tuple[SecretRenderDependency, ...] = Field(default=(), max_length=16)


class SecretRenderStatus(ObservationRecord):
    """The renderer's status document, as ``secretstatus.Status`` declares it."""

    last_attempt: SecretRenderAttempt
    last_success: SecretRenderSuccess | None = None
    connect: SecretRenderConnect | None = None


class HostRenderStatusRecord(ObservationRecord):
    """One secret renderer's account of its own runs, or why it has none.

    It holds no secret, no vault or item name and no connection ref: times,
    versions, counts and short words.
    """

    renderer: str = Field(pattern=RENDERER_NAME)
    state: Literal["read", "unreadable"]
    reason: Literal["", "missing", "unreadable", "oversized", "invalid"] = ""
    status: SecretRenderStatus | None = None


# A unit's name, one of systemd's state words, an instant in UTC and the
# title its unit file gives it. A title is the one field of words, written by
# the repository for a unit it ships and by another package for a service read
# because it restarts, so it is admitted only as printable ASCII on one line,
# without a slash or an equals sign, so no path or assignment can stand in it,
# nor the bar HQ's fact rows keep between fields. The
# controller reads each pattern from this record's schema in the bridge
# contract and checks it first. A name may carry systemd's \xNN escape, which
# a unit named for a path uses for a dash in it (run-a\x2db.mount).
UNIT_NAME = r"^(?:[A-Za-z0-9:_.@-]|\\x[0-9a-f]{2})+\.(?:service|socket|mount|automount|swap|target|path|timer|slice)$"
UNIT_WORD = r"^[a-z][a-z0-9-]{0,31}$"
UNIT_INSTANT = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
UNIT_NAME_LENGTH = 255
UNIT_TEXT = r"^[ -.0-<>-{}~]+$"
UNIT_TEXT_LENGTH = 120


_ESCAPE = re.compile(rb"\\x([0-9a-f]{2})")


def unescaped(unit: str) -> str:
    """A unit's name as a person reads it, as ``systemd-escape --unescape``
    gives it: each ``\\xNN`` its byte. Only words take it; whatever
    addresses the unit keeps the name systemd has."""

    return _ESCAPE.sub(lambda escape: bytes([int(escape[1], 16)]), unit.encode()).decode(errors="replace")


def _or_blank(pattern: str) -> str:
    """``pattern``, or nothing: a property systemd leaves unset."""

    return rf"^(?:|{pattern[1:-1]})$"


UnitName = Annotated[str, Field(pattern=UNIT_NAME, max_length=UNIT_NAME_LENGTH)]
UnitWord = Annotated[str, Field(pattern=UNIT_WORD)]
UnitInstant = Annotated[str, Field(pattern=UNIT_INSTANT)]
OptionalUnitName = Annotated[str, Field(pattern=_or_blank(UNIT_NAME), max_length=UNIT_NAME_LENGTH)]
OptionalUnitWord = Annotated[str, Field(pattern=_or_blank(UNIT_WORD))]
OptionalUnitInstant = Annotated[str, Field(pattern=_or_blank(UNIT_INSTANT))]
OptionalUnitText = Annotated[str, Field(pattern=_or_blank(UNIT_TEXT), max_length=UNIT_TEXT_LENGTH)]


class HostUnitRecord(ObservationRecord):
    """One systemd unit on the controller's machine, as `systemctl show` states
    it: the host.unit reading's record. The launcher asks systemd for exactly
    these properties of the units the repository ships and of each other
    service systemd has restarted, and the controller reports no other. Each string is a unit name, one of systemd's state words,
    an instant in UTC or the unit file's own title for a person to read; none
    is a path, a command line or an environment.
    """

    # The controller sends these members and no other; HQ drops one it does
    # not name, so nothing a unit runs, is given or reads has a field to keep.
    model_config = ConfigDict(extra="ignore", frozen=True, json_schema_extra={"additionalProperties": False})

    unit: UnitName = Field(description="Id: the unit's name.")
    title: OptionalUnitText = Field(
        default="",
        description="Description: what the unit is, in the words of the unit file the repository ships. Its name when the file has none.",
    )
    load: UnitWord = Field(
        description="LoadState: loaded, or why systemd holds no configuration for it (not-found, masked, error, bad-setting)."
    )
    file_state: OptionalUnitWord = Field(
        default="",
        description="UnitFileState: whether the unit file is enabled, static or disabled. Absent for a unit with no file.",
    )
    active: UnitWord = Field(
        description="ActiveState: active, inactive, activating, deactivating, failed and the like."
    )
    sub: UnitWord = Field(
        description="SubState: the unit type's own word for the same state, such as waiting or elapsed for a timer."
    )
    result: OptionalUnitWord = Field(
        default="", description="Result: success, or why the last run failed (exit-code, signal, timeout and the like)."
    )
    main_code: int = Field(
        default=0,
        ge=0,
        description="ExecMainCode: how the main process of the last run ended, as the kernel's code (1 exited, 2 killed, 3 dumped).",
    )
    main_status: int = Field(
        default=0,
        ge=0,
        description="ExecMainStatus: the exit status or signal number of the main process of the last run.",
    )
    started_at: OptionalUnitInstant = Field(
        default="", description="InactiveExitTimestamp: when the last start began, on this boot."
    )
    ended_at: OptionalUnitInstant = Field(
        default="", description="InactiveEnterTimestamp: when the unit last became inactive or failed, on this boot."
    )
    condition: OptionalUnitWord = Field(
        default="",
        description="ConditionResult: yes or no, whether the unit's conditions held when last checked. A start whose conditions do not hold is skipped without failing.",
    )
    condition_at: OptionalUnitInstant = Field(
        default="", description="ConditionTimestamp: when the conditions were last checked."
    )
    last_trigger_at: OptionalUnitInstant = Field(
        default="", description="LastTriggerUSec: when a timer last started its unit."
    )
    next_elapse_at: OptionalUnitInstant = Field(
        default="", description="NextElapseUSecRealtime: when a timer with a calendar schedule next elapses."
    )
    activates: OptionalUnitName = Field(default="", description="Unit: the unit a timer or path starts.")
    restarts: int = Field(
        default=0,
        ge=0,
        description="NRestarts: how often systemd restarted the service on its own since it was last started deliberately or reset.",
    )
    active_at: OptionalUnitInstant = Field(
        default="", description="ActiveEnterTimestamp: when the unit last became active, on this boot."
    )
    foreign: bool = Field(
        default=False,
        description="A service the repository does not ship, read because systemd has restarted it on its own.",
    )
    read_at: UnitInstant = Field(description="When the launcher asked systemd.")


# A mount point as the kernel's mount table spells it, its escapes undone:
# printable ASCII from a slash, without the bar HQ's fact rows keep between
# fields. The controller reads the pattern from this record's schema in the
# bridge contract and passes over a mount it does not admit.
FILESYSTEM_MOUNT = r"^/[ -{}~]*$"
FILESYSTEM_MOUNT_LENGTH = 255


class HostFilesystemRecord(ObservationRecord):
    """One real filesystem on the controller's machine, as statfs(2) states
    it: the host.filesystem reading's record. Root reads each ext4, xfs, btrfs
    or zfs mount the machine shows, one per device, and sends where it is
    mounted and its space; no device, option or file name.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, json_schema_extra={"additionalProperties": False})

    mount: str = Field(
        pattern=FILESYSTEM_MOUNT,
        max_length=FILESYSTEM_MOUNT_LENGTH,
        description="Where the filesystem is mounted, the name you find it by.",
    )
    free_bytes: int = Field(ge=0, description="f_bavail: the bytes a process that is not root can still write.")
    total_bytes: int = Field(ge=0, description="f_blocks: the filesystem's size in bytes.")
    read_at: UnitInstant = Field(description="When root asked the kernel.")


# A package as dpkg names one (Debian Policy 5.6.1): lowercase letters,
# digits, plus, minus and dots, two or more, from a letter or digit. The
# controller reads the pattern and the count from this record's schema in the
# bridge contract and passes over a name it does not admit.
REBOOT_PACKAGE = r"^[a-z0-9][a-z0-9+.-]{1,99}$"
REBOOT_PACKAGES = 64


class HostRebootRecord(ObservationRecord):
    """Whether the controller's machine waits on a restart: the
    host.reboot reading's one record. Root reads the flag the packaging
    system leaves in /run/reboot-required and the packages it lists beside it,
    and asks logind for the shutdown scheduled, if one is; no other file or
    property.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, json_schema_extra={"additionalProperties": False})

    pending: bool = Field(description="Whether /run/reboot-required is there: an update waits on a restart.")
    since: OptionalUnitInstant = Field(
        default="", description="When the flag was first written, when one is there; empty when none is."
    )
    packages: tuple[Annotated[str, Field(pattern=REBOOT_PACKAGE)], ...] = Field(
        default=(),
        max_length=REBOOT_PACKAGES,
        description="The packages /run/reboot-required.pkgs names as waiting on it, each once.",
    )
    scheduled_action: OptionalUnitWord = Field(
        default="",
        description="What logind's ScheduledShutdown will do (reboot, poweroff, halt); empty when nothing is scheduled.",
    )
    scheduled_at: OptionalUnitInstant = Field(
        default="", description="When logind will do it; empty when nothing is scheduled."
    )
    read_at: UnitInstant = Field(description="When root read the flag and asked logind.")


class HostEdgeRebootRecord(HostRebootRecord):
    """Whether an edge waits on a restart: the host.edge_reboot reading's
    record for each edge, read the same way by its receiver's reboot
    operation and handed over its SSH transport.
    """

    connection_ref: str = Field(description="The edge's SSH transport, which names the machine it reads.")


FIREWALL_KIND = "host.firewall"
PERIMETER_KIND = "host.perimeter"
RENDER_STATUS_KIND = "host.render_status"
UNIT_KIND = "host.unit"
TAILNET_LOCK_KIND = "host.tailnet_lock"
FILESYSTEM_KIND = "host.filesystem"
REBOOT_KIND = "host.reboot"
EDGE_REBOOT_KIND = "host.edge_reboot"

OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        FIREWALL_KIND,
        "host",
        "Host firewall",
        HostFirewallRecord,
        title=lambda record: str(record.get("interface", "")),
        relation="Firewall on interface",
    ),
    ObservationSpec(
        PERIMETER_KIND,
        "ssh",
        "Open-port test",
        HostPerimeterRecord,
        addresses=lambda record: tuple(record.get("public_addresses") or ()),
        title=lambda record: str(record.get("connection_ref", "")),
        relation="Open-port test through",
    ),
    ObservationSpec(
        RENDER_STATUS_KIND,
        "host",
        "Credentials refresh",
        HostRenderStatusRecord,
        title=lambda record: str(record.get("renderer", "")),
        relation="Credentials refreshed by",
    ),
    ObservationSpec(
        UNIT_KIND,
        "host",
        "Background jobs",
        HostUnitRecord,
        title=lambda record: str(record.get("title") or record.get("unit", "")),
        relation="Background jobs on",
    ),
    ObservationSpec(
        TAILNET_LOCK_KIND,
        "host",
        "Tailnet lock",
        HostTailnetLockRecord,
        title=lambda record: "On" if record.get("enabled") else "Off",
        relation="Tailnet lock seen from",
    ),
    ObservationSpec(
        FILESYSTEM_KIND,
        "host",
        "Disk space",
        HostFilesystemRecord,
        title=lambda record: str(record.get("mount", "")),
        relation="Disk space on",
    ),
    ObservationSpec(
        REBOOT_KIND,
        "host",
        "Pending restart",
        HostRebootRecord,
        title=lambda record: "Waiting on a restart" if record.get("pending") else "No restart waiting",
        relation="Restart state of",
    ),
    ObservationSpec(
        EDGE_REBOOT_KIND,
        "ssh",
        "Pending restart on an edge",
        HostEdgeRebootRecord,
        title=lambda record: "Waiting on a restart" if record.get("pending") else "No restart waiting",
        relation="Restart state of",
    ),
)
