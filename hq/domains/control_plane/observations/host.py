"""Readings a controller takes of the machine it runs on and the edges it can see."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field

from ..bridge_contract import keyword, limit
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


# A unit's name, one of systemd's state words and an instant in UTC, each as
# the bridge contract states it for the controller, which checks them first.
UNIT_NAME = keyword("HostUnitRecord", "properties", "unit", "pattern")
UNIT_WORD = keyword("HostUnitRecord", "properties", "active", "pattern")
UNIT_INSTANT = keyword("HostUnitRecord", "properties", "read_at", "pattern")

UnitName = Annotated[str, Field(pattern=UNIT_NAME, max_length=limit("HostUnitRecord", "properties", "unit", "maxLength"))]
UnitWord = Annotated[str, Field(pattern=UNIT_WORD)]
UnitInstant = Annotated[str, Field(pattern=UNIT_INSTANT)]


class HostUnitRecord(ObservationRecord):
    """One systemd unit as ``systemctl show`` states it (``HostUnitRecord`` in
    the bridge contract).

    It holds a unit's name, state words, an exit status and instants: nothing
    a unit runs, is given or reads.
    """

    unit: UnitName
    load: UnitWord
    file_state: UnitWord | Literal[""] = ""
    active: UnitWord
    sub: UnitWord
    result: UnitWord | Literal[""] = ""
    main_code: int = Field(default=0, ge=0)
    main_status: int = Field(default=0, ge=0)
    started_at: UnitInstant | Literal[""] = ""
    ended_at: UnitInstant | Literal[""] = ""
    condition: UnitWord | Literal[""] = ""
    condition_at: UnitInstant | Literal[""] = ""
    last_trigger_at: UnitInstant | Literal[""] = ""
    next_elapse_at: UnitInstant | Literal[""] = ""
    activates: UnitName | Literal[""] = ""
    read_at: UnitInstant


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
