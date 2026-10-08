"""Every address the reader and HQ answer on, one row each, as the connection page lists them."""

from dataclasses import dataclass

from hq.platform.core.network import split_host_port

from .connection import Connection
from .request_channel import channel_of


@dataclass(frozen=True)
class Address:
    """One address, what kind it is, and how HQ came to know it."""

    value: str
    kind: str
    label: str
    source: str
    current: bool = False


def _address_row(value: str, source: str, *, current: bool = False) -> Address | None:
    """An endpoint classified by the range it falls in.

    ``host:port`` and bracketed IPv6 both arrive here (the daemon writes
    endpoints that way) so the port is taken off before classifying and put
    back for display, because which port a path uses is part of the answer.
    """

    text = str(value or "").strip()
    if not text:
        return None
    bare, _port = split_host_port(text)
    channel = channel_of(bare)
    return Address(
        value=text,
        kind=channel.id,
        label={
            "tailnet": "Tailnet",
            "network": "Local network",
            "loopback": "Loopback",
        }.get(channel.id, "Public"),
        source=source,
        current=current,
    )


def addresses_of(found: Connection) -> tuple[Address, ...]:
    """Every address HQ can associate with the caller, kind by kind.

    Three different things get called "my IP" and they are rarely the same
    number: the one Tailscale issued, the one the router handed out, and the
    one the internet sees. HQ holds all three from separate places (the
    request itself, the device record, and the path the two daemons negotiated)
    and this is the only surface that puts them beside each other.
    """

    current = found.peer_address
    current_source = (
        "given by a proxy HQ does not trust, so HQ does not use it"
        if found.untrusted_forwarding
        else "this request came from it"
    )
    rows: list[Address | None] = [
        _address_row(current, current_source, current=True)
    ]
    device = found.caller_device
    if device is not None:
        rows.extend(
            _address_row(address, "issued to this device by Tailscale")
            for address in device.addresses
            if address != current
        )
    presence = found.presence
    if presence is not None:
        rows.append(
            _address_row(
                presence.direct_endpoint,
                "seen as this device's endpoint",
            )
        )
        rows.extend(
            _address_row(endpoint, "seen as one of this device's endpoints")
            for endpoint in presence.endpoints
        )
    return tuple(_deduplicated(rows))


def addresses_of_hq(found: Connection) -> tuple[Address, ...]:
    """The stable tailnet addresses assigned to the device serving HQ."""

    serves = found.serves
    if serves is None:
        return ()
    rows: list[Address | None] = [
        _address_row(address, "HQ answers here") for address in serves.addresses
    ]
    return tuple(_deduplicated(rows))


def _deduplicated(rows) -> list[Address]:
    """One row per address, with the ports it was seen on folded into it.

    A node reports the same address on several ports, and listing each as its
    own row turns four facts into a dozen lines that all say the same thing.
    """

    found: dict[str, Address] = {}
    ports: dict[str, list[str]] = {}
    for row in rows:
        if row is None:
            continue
        host, port = split_host_port(row.value)
        if host not in found:
            found[host] = row
            ports[host] = []
        if port and port not in ports[host]:
            ports[host].append(port)
    return [
        Address(
            value=f"{host}:{'/'.join(ports[host])}" if ports[host] else host,
            kind=row.kind,
            label=row.label,
            source=row.source,
            current=row.current,
        )
        for host, row in found.items()
    ]
