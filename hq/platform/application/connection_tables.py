"""The connection page's tables: what HQ could not read, the addresses on
each side, the headers each way, and what a hop's request carried."""

from typing import Any

lazy from hq.platform.core.included_parts import RESPONSE_CHECK

from .tables import Cell, DataTable, Piece, Row, StatusMark, TableColumn
from .ui import ended


def _unread(result: Any) -> Row:
    """A check HQ could not judge, its reason and how to fix it."""

    reading = result.reading
    if reading.fix:
        reason = Cell(f"{reading.evidence}:", role="prose", parts=(Cell(ended(reading.fix), reading.fix_url),))
    else:
        reason = Cell(ended(reading.evidence), role="prose")
    return Row((Cell(result.check.label, name=True), reason), data={"posture-unread": result.check.id})


def unread(posture: Any) -> DataTable:
    """Each posture check HQ could not judge, with why."""

    rows = tuple(_unread(result) for result in posture.results if result.state == "unavailable")
    return DataTable("Not read", (TableColumn("Check"), TableColumn("Reason")), rows)


def evidence(hop: Any) -> DataTable:
    """What the request itself carried at ``hop``: its value, kind and note."""

    columns = (
        TableColumn("Value", kind="identifier", cell=lambda item: Cell(item.value, readout=True)),
        TableColumn(
            "Type", kind="status", cell=lambda item: Cell(status=StatusMark(item.role or item.label, tone=item.tone))
        ),
        TableColumn("Note", drop=1, cell=lambda item: Cell(item.detail)),
    )
    return DataTable.of("Evidence", columns, hop.evidence)


def addresses(label: str, found: Any, side: str, *, known: bool) -> DataTable:
    """Each address on ``side`` (``yours`` or ``hq``) and its network, with
    where HQ knows it from where it is ``known``."""

    columns = (
        TableColumn("Address", kind="identifier", cell=lambda address: Cell(address.value, readout=True)),
        TableColumn(
            "Network", kind="status", cell=lambda address: Cell(status=StatusMark(address.label, tone=address.tone))
        ),
        *((TableColumn("Known from", cell=lambda address: Cell(address.source)),) if known else ()),
    )
    return DataTable.of(label, columns, found, data={"addresses": side})


def _header(header: Any) -> Cell:
    return Cell(f"{header.name}: {header.value}", readout=True)


def headers_read(path: Any) -> DataTable:
    """The headers HQ read, and what each is read for."""

    columns = (
        TableColumn("Header", cell=_header),
        TableColumn("HQ", kind="status", cell=lambda header: Cell(status=StatusMark(header.state))),
        TableColumn("For", drop=1, cell=lambda header: Cell(header.purpose, role="prose")),
    )
    used = (header for header in path.headers if header.used)
    return DataTable.of("Headers HQ read", columns, used, data={"request-headers": "read"})


def headers_unread(path: Any) -> DataTable:
    """The headers HQ declined or ignored, and why."""

    columns = (
        TableColumn("Header", cell=_header),
        TableColumn("HQ", kind="status", drop=2, cell=lambda header: Cell(status=StatusMark(header.state))),
        TableColumn("Reason", drop=1, cell=lambda header: Cell(header.declined, role="prose")),
    )
    return DataTable.of("Unread headers", columns, path.unread_headers, data={"request-headers": "unread"})


def _sent(check: Any) -> Row:
    """One thing the browser reads back, which connection.js fills in."""

    def piece(**side: bool) -> Cell:
        return Cell(piece=Piece(RESPONSE_CHECK, {"check": check, **side}, words=check.name))

    data = {"response-check": check.name, **({"response-directive": ""} if check.directive else {})}
    return Row((piece(value=True), piece(value=False), Cell(check.purpose)), data=data)


def headers_sent(path: Any) -> DataTable:
    """What HQ sends back, each row read from a real response once its fold opens."""

    columns = (TableColumn("Header"), TableColumn("HQ", kind="status"), TableColumn("For", drop=1))
    rows = tuple(_sent(check) for check in path.response_checks)
    return DataTable("Headers HQ sent", columns, rows, data={"response-headers": ""})


def panel(posture: Any, path: Any) -> dict[str, DataTable]:
    """Every table of the connection panel but a hop's, which its hop draws."""

    return {
        "unread": unread(posture),
        "yours": addresses("Your addresses", path.addresses, "yours", known=True),
        "hq": addresses("HQ answers at", path.hq_addresses, "hq", known=False),
        "read": headers_read(path),
        "declined": headers_unread(path),
        "sent": headers_sent(path),
    }
