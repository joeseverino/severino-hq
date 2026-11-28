"""The Agents page's tables: the devices waiting for approval, each agent,
each agent's devices, the reader's own apps, and what the MCP endpoint offers."""

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from django.utils.text import capfirst

from hq.platform.application.agent_activity import CLIENT_SETUP, Activity, AgentStanding, DeviceRow, Streams
from hq.platform.application.moments import when
from hq.platform.application.tables import Below, Cell, DataTable, Group, Row, StatusMark, TableColumn
from hq.platform.core.templatetags.value_tags import agreed, counted, interface_label, measured


def prompts(found: Sequence[tuple[Any, Any]]) -> DataTable:
    """Each device waiting for the reader's approval, linked to its row below."""

    rows = tuple(
        Row(
            (Cell(prompt.text, f"#{prompt.device.anchor}", header=True, role="data"),),
            actions=actions,
            data={"prompt": prompt.device.anchor},
        )
        for prompt, actions in found
    )
    return DataTable("Needs approval", (TableColumn("Device"),), rows)


_AGENT_COLUMNS = (
    TableColumn("Agent", kind="identifier"),
    TableColumn("Permissions", drop=2),
    TableColumn("Last seen", kind="status", drop=3),
    TableColumn("Activity", drop=1),
)


def agents(found: Sequence[tuple[AgentStanding, Any]]) -> DataTable:
    """Each agent HQ has seen: what it is, what of its grant HQ lets it use,
    when it was last seen and what it did, and in its detail what it holds."""

    return DataTable("Agents", _AGENT_COLUMNS, tuple(_agent(standing, actions) for standing, actions in found))


def _agent(standing: AgentStanding, actions: Any) -> Row:
    identity = standing.identity
    data = {"agent": standing.subject, "client": identity.client_id}
    blocked = (Cell(status=StatusMark("Blocked", tone="attention")),) if identity.blocked_at else ()
    if identity.blocked_at:
        data["blocked"] = ""
    name = Cell(
        quoted=f"`{identity.client_id}`",
        header=True,
        parts=blocked,
        lines=_dotted(_about(standing)),
        below=_held(standing),
    )
    # Of what it may hold: in force, and offered but not granted, which the line below counts.
    granted = [Cell(f"{len(standing.in_force)} of {len(standing.in_force) + len(standing.missing)}")]
    if standing.beyond_role:
        granted.append(Cell(f"{len(standing.beyond_role)} beyond its person's role", muted=True))
    if standing.missing:
        granted.append(Cell(status=StatusMark(len(standing.missing), "not granted", tone="attention", pill=False)))
    cells = (
        name,
        Cell(lines=tuple(granted), sort=str(len(standing.in_force))),
        Cell(moment=identity.last_seen),
        _activity(standing.activity, standing.streams),
    )
    return Row(cells, actions=actions, data=data)


def _about(standing: AgentStanding) -> list[Cell]:
    """What an agent is, said under its name: whom it acts for, how it signs
    in, what it calls over, and the devices it called from."""

    identity = standing.identity
    said = []
    if person := identity.person:
        role = standing.role_label.lower() if standing.role else "no role"
        said.append(Cell(f"For {person.get_username()}, {role}", data={"person": ""}))
    elif identity.signs_in:
        said.append(Cell(f"Signs in {identity.get_signs_in_display().lower()}"))
    if identity.interfaces:
        said.append(Cell(f"Over {interface_label(identity.interfaces)}"))
    if standing.devices:
        said.append(Cell(f"From {counted(len(standing.devices), 'device')}", f"#devices-{identity.pk}"))
    return said


def _held(standing: AgentStanding) -> tuple[Below, ...]:
    """What its row opens to: its block, its person's role, the streams it
    holds open, and the permissions it holds, holds beyond its role or lacks."""

    identity = standing.identity
    lines = []
    if identity.blocked_at:
        lines.append(Cell(_by(f"Blocked {when(identity.blocked_at)}", identity.blocked_by)))
    if person := identity.person:
        if standing.acts_for_nobody:
            said = f"{person.get_username()} holds no role, so its calls are refused."
            lines.append(Cell(said, data={"role": "none"}))
        else:
            confirmed = Cell(moment=standing.role_confirmed)
            lines.append(Cell("Role confirmed", parts=(confirmed,), data={"role": standing.role}))
    streams = standing.streams
    if streams.open and not streams.full:
        said = f"{streams.open} of {counted(streams.cap, 'live update stream,live update streams')} open."
        lines.append(Cell(said, data={"streams": "open"}))
    if standing.in_force:
        lines.append(Cell("Permissions:", parts=_codes(standing.in_force)))
    if standing.beyond_role:
        said = f"Granted, but beyond {identity.person.get_username()}'s role:"
        lines.append(Cell(said, parts=_codes(standing.beyond_role)))
    if standing.missing:
        lines.append(Cell("Not granted:", parts=_codes(standing.missing), data={"missing": ""}))
        grant = (
            _code(identity.client_id),
            Cell("in your sign-in service, then sign it in again."),
            Cell("Client setup", CLIENT_SETUP, external=True),
        )
        lines.append(Cell(f"Grant {agreed(len(standing.missing), 'it,them')} to", parts=grant, data={"missing": ""}))
    return (Below("", tuple(lines)),) if lines else ()


def _codes(names: Iterable[str]) -> tuple[Cell, ...]:
    return tuple(_code(name) for name in names)


def _code(name: str) -> Cell:
    """A name drawn as code whatever its words, as a permission's and a client's are."""

    return Cell(quoted=f"`{name}`")


def _by(said: str, person: Any) -> str:
    return f"{said} by {person.get_username()}." if person else f"{said}."


def _dotted(said: list[Cell]) -> tuple[Cell, ...]:
    """Words said in a muted line, each a whole part past a dot."""

    return (Cell(muted=True, fit=True, parts=tuple(said)),) if said else ()


def _activity(activity: Activity, streams: Streams | None) -> Cell:
    """What an agent or one of its devices did over the window: its changes,
    what was refused and why, and a full set of streams."""

    lines = [Cell(counted(activity.changes, "change"))]
    if activity.refused:
        refused = StatusMark(measured(activity.refused, "refused"), tone="attention", pill=False)
        too_much = "for calling too much"
        if activity.limited and activity.limited != activity.refused:
            too_much = f"{activity.limited} {too_much}"
        reason = (Cell(too_much, muted=True),) if activity.limited else ()
        lines.append(Cell(status=refused, parts=reason))
    if streams is not None and streams.full:
        full = StatusMark(
            streams.open,
            "of",
            counted(streams.cap, "stream"),
            "open",
            tone="attention",
            pill=False,
            data_streams="full",
        )
        lines.append(Cell(status=full, parts=(Cell("so another is refused", muted=True),)))
    return Cell(lines=tuple(lines), sort=str(activity.changes))


_DEVICE_COLUMNS = (
    TableColumn("Device", kind="identifier"),
    TableColumn("Approval", kind="status"),
    TableColumn("First seen", kind="status", drop=2),
    TableColumn("Last seen", kind="status", drop=3),
    TableColumn("Activity", drop=1),
)


def devices(found: Sequence[tuple[AgentStanding, Sequence[tuple[DeviceRow, Any]]]]) -> DataTable:
    """The tailnet devices each agent called from, a group per agent that has any."""

    groups = tuple(
        Group(
            standing.label,
            tuple(_device(device, actions) for device, actions in rows),
            counted=True,
            id=f"devices-{standing.identity.pk}",
            data={"agent-devices": standing.subject},
        )
        for standing, rows in found
    )
    return DataTable("Devices", _DEVICE_COLUMNS, groups=groups)


def _device(device: DeviceRow, actions: Any) -> Row:
    said = [Cell(device.os)] if device.os else []
    if device.owner:
        said.append(Cell(f"Owned by {device.owner}", data={"owner": ""}))
    elif device.record:
        said.append(Cell("Tagged"))
    elif device.refusal:
        said.append(Cell(capfirst(device.refusal.said), data={"unplaced": ""}))
    name = Cell(device.name, header=True, lines=_dotted(said), below=_record(device.record))
    approval = [Cell(status=StatusMark(device.standing))]
    if device.waits_for:
        approval.append(Cell(f"For {device.waits_for}", muted=True))
    cells = (
        name,
        Cell(lines=tuple(approval)),
        Cell(moment=device.first_seen),
        Cell(moment=device.last_seen),
        _activity(device.activity, None),
    )
    return Row(cells, actions=actions, id=device.anchor, data={"device-standing": str(device.standing)})


def _record(record: Any) -> tuple[Below, ...]:
    """Whom a device's calls are for, who approves it, and when it was approved or blocked."""

    if record is None:
        return ()
    if record.person:
        lines = [Cell(f"Calls for {record.person.get_username()}, who approves it.")]
    else:
        lines = [Cell("Calls as itself, so anyone who governs agents approves it.")]
    if record.approved_at:
        lines.append(Cell(_by(f"Approved {when(record.approved_at)}", record.approved_by)))
    if record.blocked_at:
        lines.append(Cell(_by(f"Blocked {when(record.blocked_at)}", record.blocked_by)))
    return (Below("", tuple(lines)),)


def own_devices(found: Sequence[tuple[Any, Any]]) -> DataTable:
    """The signed-in person's own apps, each removable from its row."""

    columns = (TableColumn("App", kind="identifier"), TableColumn("Registered", kind="status"))
    rows = tuple(
        Row(
            (replace(_code(device.client_id), header=True), Cell(moment=device.registered_at)),
            actions=actions,
            data={"device": device.client_id},
        )
        for device, actions in found
    )
    return DataTable("Your apps", columns, rows, region={"empty": "None registered."})


def offers(endpoint: Any) -> DataTable:
    """What the MCP endpoint offers, a group for each kind: each by its name
    and whether it only reads, what it says of itself, and the rest of it on
    its open line."""

    columns = (TableColumn("Name", kind="identifier"), TableColumn("Description"))
    groups = tuple(
        Group(offer.label, tuple(_offered(item) for item in offer.items), counted=True) for offer in endpoint.offers
    )
    return DataTable("MCP server", columns, groups=groups)


def _offered(item: Any) -> Row:
    reads = (Cell("Reads only", muted=True, dot=True),) if item.reads_only else ()
    more = (Below("", tuple(Cell(quoted=paragraph) for paragraph in item.more)),) if item.more else ()
    name = replace(_code(item.name), header=True, parts=reads, below=more)
    summary = Cell(quoted=item.summary, role="data")
    if item.title:
        summary = Cell(item.title, name=True, role="data", parts=(Cell(quoted=item.summary),))
    return Row((name, summary))
