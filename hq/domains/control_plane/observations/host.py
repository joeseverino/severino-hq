"""Readings a controller takes of the machine it runs on and the edges it can see."""

import re
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, ConfigDict, Field

from .contract import ObservationRecord, ObservationSpec


class HostFirewallRecord(ObservationRecord):
    record: str
    interface: str = ""
    accept_requires_interface: bool = False
    foreign_interface_dropped: bool = False
    read_at: str = ""


class HostPerimeterRecord(ObservationRecord):
    record: str
    connection_ref: str
    firewall_unit: str = "unknown"
    public_addresses: tuple[str, ...] = ()
    ports_checked: tuple[int, ...] = ()
    answered_publicly: tuple[int, ...] = ()
    read_at: str = ""


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


def _instant(value: Any) -> str:
    """An ISO 8601 instant as it was written, or "" when it is not one."""

    from hq.platform.application.timestamps import moment

    return value if isinstance(value, str) and moment(value) is not None else ""


Word = Annotated[str, BeforeValidator(_word)]
Instant = Annotated[str, BeforeValidator(_instant)]


class SecretRenderAttempt(ObservationRecord):
    at: Instant = ""
    outcome: Word = UNREADABLE_WORD
    failure: Word = ""


class SecretRenderCounts(ObservationRecord):
    items_read: int = 0
    connections: int = 0
    app_variables: int = 0
    identities: int = 0
    signing_keys: int = 0


class SecretRenderSuccess(ObservationRecord):
    at: Instant = ""
    rendered_at: Instant = ""
    content_version: int | None = None
    attribute_version: int | None = None
    counts: SecretRenderCounts = SecretRenderCounts()


class SecretRenderDependency(ObservationRecord):
    service: Word
    status: Word


class SecretRenderConnect(ObservationRecord):
    read_at: Instant = ""
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


# A unit's name, one of systemd's state words and an instant in UTC. The
# controller reads each pattern from this record's schema in the bridge
# contract and checks it first.
UNIT_NAME = r"^[A-Za-z0-9:_.@-]+\.(?:service|socket|mount|automount|swap|target|path|timer|slice)$"
UNIT_WORD = r"^[a-z][a-z0-9-]{0,31}$"
UNIT_INSTANT = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
UNIT_NAME_LENGTH = 255


def _or_blank(pattern: str) -> str:
    """``pattern``, or nothing: a property systemd leaves unset."""

    return rf"^(?:|{pattern[1:-1]})$"


UnitName = Annotated[str, Field(pattern=UNIT_NAME, max_length=UNIT_NAME_LENGTH)]
UnitWord = Annotated[str, Field(pattern=UNIT_WORD)]
UnitInstant = Annotated[str, Field(pattern=UNIT_INSTANT)]
OptionalUnitName = Annotated[str, Field(pattern=_or_blank(UNIT_NAME), max_length=UNIT_NAME_LENGTH)]
OptionalUnitWord = Annotated[str, Field(pattern=_or_blank(UNIT_WORD))]
OptionalUnitInstant = Annotated[str, Field(pattern=_or_blank(UNIT_INSTANT))]


class HostUnitRecord(ObservationRecord):
    """One systemd unit on the controller's machine, as `systemctl show` states
    it: the host.unit reading's record. The launcher asks systemd for exactly
    these properties of the units the repository ships, and the controller
    reports no other. Each string is a unit name, one of systemd's state words
    or an instant in UTC; none is a path, a command line or an environment.
    """

    # The controller sends these members and no other; HQ drops one it does
    # not name, so nothing a unit runs, is given or reads has a field to keep.
    model_config = ConfigDict(extra="ignore", frozen=True, json_schema_extra={"additionalProperties": False})

    unit: UnitName = Field(description="Id: the unit's name.")
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
    read_at: UnitInstant = Field(description="When the launcher asked systemd.")


FIREWALL_KIND = "host.firewall"
PERIMETER_KIND = "host.perimeter"
RENDER_STATUS_KIND = "host.render_status"
UNIT_KIND = "host.unit"

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
        title=lambda record: str(record.get("unit", "")),
        relation="Background jobs on",
    ),
)
