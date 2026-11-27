"""Readings a controller takes of the machine it runs on and the edges it can see."""

import re
from datetime import timedelta
from typing import Annotated, Any, Literal, get_args

from pydantic import ConfigDict, Field

from hq.platform.application.reasons import Checked
lazy from hq.platform.application.labels import human_bytes

from ..provider_spec import InboundSpec, SharedValue
from .contract import ObservationRecord, ObservationSpec, Stamp
from .portainer import CONTAINER_KIND, ENVIRONMENT_KIND, IMAGE_KIND, RUNTIME_KIND


class HostFirewallRecord(ObservationRecord):
    record: str
    interface: str = ""
    accept_requires_interface: bool = False
    foreign_interface_dropped: bool = False
    read_at: Stamp = None


# A tailnet lock public key as the lock commands take it: "tlpub:" and the
# key's hex.
LOCK_KEY = r"^tlpub:[0-9a-f]{1,128}$"


class TailnetLockSigner(ObservationRecord):
    """One key tailnet lock trusts to sign: the public half a device holds,
    the kind of key it is, its votes and the labels it was added with."""

    key: str = Field(pattern=LOCK_KEY)
    kind: str = Field(default="", max_length=32)
    votes: int = Field(default=0, ge=0)
    metadata: dict[str, str] = Field(default={}, max_length=16)


class HostTailnetLockRecord(ObservationRecord):
    """Tailnet lock as the controller's own machine sees it: `tailscale lock
    status`, with each key it trusts to sign."""

    record: str
    enabled: bool = False
    node_key_signed: bool = False
    signers: tuple[TailnetLockSigner, ...] = Field(default=(), max_length=64)
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
# A renderer's name is a fixed word, never a path.
RENDERER_NAME = r"^[a-z][a-z0-9-]{0,31}$"
# The units of HQ's host a finding tells an operator to start or read, as
# deploy/systemd names them, and each secret renderer the launcher names, by
# the unit that runs it. The host's layout takes them from the contract.
CONTROLLER_UNIT = "severino-hq-controller.service"
SECRETS_UNIT = "severino-hq-secrets.service"
RENDERER_UNITS = {"hq": SECRETS_UNIT}
# Every word a renderer files a failed run under (``Class`` in
# controller/secrets, whose constants are generated from the contract), and
# what each means.
UNKNOWN_FAILURE = "an error HQ does not recognize"
FAILURE_CLASSES = {
    "config": "the job's configuration was refused",
    "host": "a folder, mount, lock file or destination on the machine is not as it should be",
    "busy": "another refresh was already running",
    "connect_unavailable": "1Password Connect did not answer in time, or the vault kept changing",
    "connect_denied": "1Password Connect refused the token",
    "connect_response": "1Password Connect gave an answer HQ could not use",
    "content": "something in the vault was refused",
    "web_unhealthy": "the web container did not come back after its settings changed",
    "internal": UNKNOWN_FAILURE,
}
# What a record says of the read itself, and why a document could not be read.
RenderState = Literal["read", "unreadable"]
RENDER_READ, RENDER_UNREADABLE = get_args(RenderState)
RenderReason = Literal["", "missing", "unreadable", "oversized", "invalid"]
RENDER_REASONS = tuple(reason for reason in get_args(RenderReason) if reason)
# The longest a renderer whose vault is unchanged goes without reading it in full.
FULL_READ_EVERY = timedelta(hours=24)

_STATUS_WORD = re.compile(STATUS_WORD)


def _word(value: Any) -> str:
    return value if isinstance(value, str) and _STATUS_WORD.fullmatch(value) else UNREADABLE_WORD


Word = Annotated[str, Checked(_word, mode="before")]


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
    state: RenderState
    reason: RenderReason = ""
    status: SecretRenderStatus | None = None


# A unit's name, one of systemd's state words, an instant in UTC and the
# title its unit file gives it. A title is the one field of words, written by
# the repository for a unit it ships and by another package for a service read
# because it restarts, so it is admitted only as printable ASCII on one line,
# without a slash or an equals sign, so no path or assignment can stand in it.
# A status is the other: what HQ's own program says as it fails, kept for the
# units the repository ships alone.
# The controller reads each pattern from this record's schema in the bridge
# contract and checks it first. A name may carry systemd's \xNN escape, which
# a unit named for a path uses for a dash in it (run-a\x2db.mount).
UNIT_NAME = r"^(?:[A-Za-z0-9:_.@-]|\\x[0-9a-f]{2})+\.(?:service|socket|mount|automount|swap|target|path|timer|slice)$"
UNIT_WORD = r"^[a-z][a-z0-9-]{0,31}$"
UNIT_INSTANT = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
UNIT_NAME_LENGTH = 255
UNIT_TEXT = r"^[ -.0-<>-~]+$"
UNIT_TEXT_LENGTH = 120
# What HQ's own program said as it failed (sd_notify STATUS=): one line of
# printable ASCII, which may name the image or directory it found missing
# and never assigns a value. The controller keeps it only for a unit the
# repository ships.
UNIT_STATUS = r"^[ -<>-~]+$"
UNIT_STATUS_LENGTH = 400
# The most units one unit can require that HQ keeps.
UNIT_REQUIRES = 32


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
OptionalUnitStatus = Annotated[str, Field(pattern=_or_blank(UNIT_STATUS), max_length=UNIT_STATUS_LENGTH)]


class HostUnitRecord(ObservationRecord):
    """One systemd unit on the controller's machine, as `systemctl show` states
    it: the host.unit reading's record. The launcher asks systemd for exactly
    these properties of the units the repository ships and of each other
    service systemd has restarted, and the controller reports no other. Each string is a unit name, one of systemd's state words,
    an instant in UTC, the unit file's own title for a person to read, or
    what HQ's own program said of a shipped unit as it failed; none is a
    command line or an environment, and only that last may name a path.
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
        description="ExecMainCode: how the last run's main process ended, as the kernel's code (1 exited, 2 killed, 3 dumped).",
    )
    main_status: int = Field(
        default=0,
        ge=0,
        description="ExecMainStatus: the exit status or signal of the last run's main process.",
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
    requires: tuple[UnitName, ...] = Field(
        default=(),
        max_length=UNIT_REQUIRES,
        description=(
            "Requires, and the mount unit of each path in RequiresMountsFor: the units this one cannot start without."
        ),
    )
    status: OptionalUnitStatus = Field(
        default="",
        description=(
            "StatusText: what a unit the repository ships last told systemd of itself, such as why its run failed. "
            "systemd keeps it until the unit next starts."
        ),
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
        keys=lambda record: tuple(str(signer.get("key", "")) for signer in record.get("signers") or ()),
        relation="Tailnet lock seen from",
        key_relation="Signs for tailnet lock",
    ),
    ObservationSpec(
        FILESYSTEM_KIND,
        "host",
        "Disk space",
        HostFilesystemRecord,
        title=lambda record: str(record.get("mount", "")),
        describe=lambda record: (
            f"{human_bytes(record.get('free_bytes', 0))} free of {human_bytes(record.get('total_bytes', 0))}"
        ),
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

# What HQ's own host jobs say happened: a unit that failed, which every
# severino-hq unit's OnFailure= reports (``host_work.units.read``); a job
# systemd ended on HQ's host, for any unit, with its result after a dot; and a
# container on HQ's host that changed, with Docker's action after a dot, which
# makes due the container list, each one's inspect and the images pulled, since
# a sweep of only the environments reads none of them again. The last two are
# what severino-hq-events.service hears.
UNIT_FAILED = "unit.failed"
UNIT_JOB = "unit.job"
DOCKER_CONTAINER = "docker.container"
# The runs held on a unit their playbook left running on HQ's host, which HQ
# reads itself by asking each its settling read (``host_work``).
AUTOMATION_HELD = "automation.held"
# Each end of a job HQ is told of, by systemd's result, and what it makes due:
# any end may be a held run's unit's, and one that did not succeed leaves its
# unit failed, which the machine's units say.
JOB_ENDS = {
    "done": (AUTOMATION_HELD,),
    **dict.fromkeys(("failed", "timeout", "canceled"), (UNIT_KIND, AUTOMATION_HELD)),
}
INBOUND = {
    "host": InboundSpec(
        {
            UNIT_FAILED: (UNIT_KIND,),
            **{f"{UNIT_JOB}.{result}": kinds for result, kinds in JOB_ENDS.items()},
            DOCKER_CONTAINER: (ENVIRONMENT_KIND, CONTAINER_KIND, RUNTIME_KIND, IMAGE_KIND),
        }
    )
}
# The events severino-hq-events.service tells, and the ends of a job it tells,
# which the controller spells from the contract.
SHARED = (
    SharedValue("HostEvent", (UNIT_JOB, DOCKER_CONTAINER), "An event HQ's own host tells it of from the controller."),
    SharedValue("HostJobResult", tuple(JOB_ENDS), "How a job HQ's host tells it of ended, as systemd says it."),
    SharedValue("UnitName", UnitName, "A systemd unit's name, as systemd spells it."),
    SharedValue(
        "StatusWord",
        Annotated[str, Field(pattern=STATUS_WORD)],
        "Every string a secret renderer's status document carries besides its times.",
    ),
    SharedValue(
        "HostService",
        (CONTROLLER_UNIT, SECRETS_UNIT),
        "A unit of HQ's host that HQ tells an operator to start or read.",
        varnames=("ControllerUnit", "SecretsUnit"),
    ),
    SharedValue("SecretRenderer", tuple(RENDERER_UNITS), "A secret renderer the host's launcher names."),
    SharedValue("RenderFailure", tuple(FAILURE_CLASSES), "A word a secret renderer files a failed run under."),
    SharedValue(
        "RenderFullReadSeconds",
        Annotated[int, Field(default=int(FULL_READ_EVERY.total_seconds()))],
        "The longest, in seconds, a secret renderer whose vault is unchanged goes without reading it in full.",
    ),
)
