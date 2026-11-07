"""tailscaled's LocalAPI on its Unix socket: which device is at an address, asked per request.

The one live read of the tailnet. ``whois`` answers from the daemon's own
netmap, so the device is the WireGuard peer that opened the connection, with
no polled reading between them to go stale. Only an agent's admission asks
(``agent_devices.place``); a page never does. Where the socket is absent (a
developer's Mac, a deployment without Tailscale) HQ has no tailnet to place a
call on, and says so (docs/Security.md#agents).
"""

import http.client
import json
import socket
from contextlib import closing
from typing import Any
from urllib.parse import quote

from django.conf import settings

from hq.platform.core.outbound import allowed

from .tailnet import Device

# The host the daemon's handler accepts for a request on its socket.
LOCAL_HOST = "local-tailscaled.sock"
# A call on a loopback socket to a daemon that is up answers in microseconds;
# one that takes longer is not answering.
TIMEOUT = 2.0
LIMIT = 1 << 20


class Unavailable(Exception):
    """tailscaled did not answer: no socket to connect to, a refusal, or an unreadable answer."""


def socket_path() -> str:
    """Where tailscaled's socket is, as the host said; "" where the deployment has no tailnet.

    The host's word, never the socket's presence: a daemon restarting on a
    tailnet host leaves no socket for a moment, and a call then waits
    (``Unavailable``) rather than running unplaced.
    """

    return str(settings.SEVERINO_TAILSCALED_SOCKET or "").strip()


def whois(address: str) -> Device | None:
    """The device at ``address`` as the daemon knows it now, or None when no peer is there.

    Raises ``Unavailable`` when the daemon did not answer the question.
    """

    try:
        status, body = get(f"/localapi/v0/whois?addr={quote(address, safe='')}")
    except (OSError, http.client.HTTPException) as exc:
        raise Unavailable(str(exc) or type(exc).__name__) from exc
    # The daemon's own word for an address it holds no node for (client/local, WhoIs).
    if status == http.HTTPStatus.NOT_FOUND:
        return None
    if status != http.HTTPStatus.OK:
        raise Unavailable(f"tailscaled answered {status}")
    try:
        answer = json.loads(body)
    except ValueError as exc:
        raise Unavailable("tailscaled's answer was not JSON") from exc
    if not isinstance(answer, dict) or not isinstance(answer.get("Node"), dict):
        raise Unavailable("tailscaled's answer named no node")
    return _device(address, answer)


def get(path: str) -> tuple[int, bytes]:
    """One GET of ``path`` on the daemon's socket: its status and body."""

    with closing(_Connection(socket_path())) as daemon, allowed("tailscaled"):
        daemon.request("GET", path)
        response = daemon.getresponse()
        return response.status, response.read(LIMIT)


class _Connection(http.client.HTTPConnection):
    """HTTP over the daemon's Unix socket, under the host name its handler accepts."""

    def __init__(self, path: str) -> None:
        super().__init__(LOCAL_HOST, timeout=TIMEOUT)
        self.socket_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _device(address: str, answer: dict[str, Any]) -> Device:
    """``apitype.WhoIsResponse`` as the device HQ places a call on."""

    node = answer["Node"]
    profile = answer.get("UserProfile") or {}
    tags = tuple(str(tag) for tag in node.get("Tags") or ())
    name = str(node.get("ComputedName") or "").strip() or str(node.get("Name") or "").strip(".").split(".", 1)[0]
    return Device(
        name=name,
        node_id=str(node.get("StableID") or ""),
        # A tagged device is nobody's; the profile then names Tailscale's placeholder login.
        user="" if tags else str(profile.get("LoginName") or ""),
        tags=tags,
        addresses=(address,),
        dns_name=str(node.get("Name") or "").rstrip("."),
        os=str((node.get("Hostinfo") or {}).get("OS") or ""),
        online=True,
    )
