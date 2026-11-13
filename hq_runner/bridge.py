"""The worker's calls to HQ: claim, renew and report, over HQ's bridge socket.

The Go controller's client (``controller/runtime/bridge.go``) in Python, with
the same trust: the socket's directory and the socket itself belong to this
account and are open to no other, and the peer answering is this account, so
a socket swapped in after the check is still refused. One connection per call,
no redirect followed, an answer read only up to the bridge's bound and taken
only as JSON.
"""

import http.client
import json
import os
import socket
import stat
import struct
import sys
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, override
from urllib.parse import urlencode

# The bridge contract's BridgeBody maxLength: the most bytes either side sends.
MAX_BRIDGE_BODY = 64 << 20
# How long one call may take before the worker stops waiting for it.
BRIDGE_TIMEOUT_SECONDS = 180
# How much of a refusal's detail is quoted in the error it becomes.
_DETAIL_LIMIT = 300


class BridgeError(Exception):
    """A call HQ refused, or one that got no answer the contract allows."""


def peer_uid(connection: socket.socket) -> int:
    """The account the kernel says is at the other end of ``connection``."""

    if sys.platform == "linux":
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        uid = struct.unpack("3i", raw)[1]
    elif sys.platform == "darwin":
        # LOCAL_PEERCRED at SOL_LOCAL (0) answers a struct xucred: its version,
        # then the uid.
        raw = connection.getsockopt(0, 1, struct.calcsize("Ii") + 4 * 18)
        uid = struct.unpack_from("Ii", raw)[1]
    else:
        raise BridgeError("peer credentials are not available on this platform")
    return int(uid)


def trusted(path: str, uid: int) -> None:
    """Refuse a socket at ``path`` this account should not trust."""

    if not PurePosixPath(path).is_absolute() or os.path.normpath(path) != path:
        raise BridgeError("the bridge socket path is not an absolute, clean path")
    parent = str(PurePosixPath(path).parent)
    try:
        directory = os.lstat(parent)
    except OSError as exc:
        raise BridgeError("HQ is not serving the bridge: its socket directory is missing") from exc
    if not stat.S_ISDIR(directory.st_mode):
        raise BridgeError("the bridge socket's directory is not a directory")
    if directory.st_uid != uid:
        raise BridgeError("the bridge socket's directory belongs to another account")
    if directory.st_mode & 0o077:
        raise BridgeError("the bridge socket's directory is open to another account")
    try:
        found = os.lstat(path)
    except FileNotFoundError as exc:
        raise BridgeError("HQ is not serving the bridge: no socket at its path") from exc
    except OSError as exc:
        raise BridgeError("the bridge socket could not be examined") from exc
    if not stat.S_ISSOCK(found.st_mode):
        raise BridgeError("the bridge socket path is not a socket")
    if found.st_uid != uid:
        raise BridgeError("the bridge socket belongs to another account")
    if stat.S_IMODE(found.st_mode) != 0o600:
        raise BridgeError("the bridge socket's mode is not 0600")


class _Connection(http.client.HTTPConnection):
    """An HTTP connection to the bridge socket, checked as it is made."""

    def __init__(self, path: str, uid: int, timeout: float) -> None:
        # The socket decides where a request goes; the name only fills Host.
        super().__init__("hq-bridge", timeout=timeout)
        self._path = path
        self._uid = uid

    @override
    def connect(self) -> None:
        trusted(self._path, self._uid)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self._path)
        except OSError as exc:
            connection.close()
            raise BridgeError("HQ is not listening on the bridge socket") from exc
        try:
            peer = peer_uid(connection)
        except OSError as exc:
            connection.close()
            raise BridgeError("the bridge socket's peer could not be read") from exc
        if peer != self._uid:
            connection.close()
            raise BridgeError("the bridge socket is answered by another account")
        self.sock = connection


class Bridge:
    """HQ's bridge at a socket path, reached as this account."""

    def __init__(self, path: str, *, uid: int | None = None, timeout: float = BRIDGE_TIMEOUT_SECONDS) -> None:
        if not path:
            raise BridgeError("no bridge socket is named")
        self._path = path
        self._uid = os.geteuid() if uid is None else uid
        self._timeout = timeout

    def call(self, action: str, query: Mapping[str, Any], payload: Any = None) -> dict[str, Any]:
        """Run one bridge action and answer its JSON object."""

        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            if len(body) > MAX_BRIDGE_BODY:
                raise BridgeError("bridge payload is too large")
        connection = _Connection(self._path, self._uid, self._timeout)
        try:
            try:
                connection.request(
                    "POST",
                    f"/{action}?{urlencode(query, doseq=True)}",
                    body=body,
                    headers={"Content-Type": "application/json"} if body is not None else {},
                )
                response = connection.getresponse()
                answer = response.read(MAX_BRIDGE_BODY + 1)
            except BridgeError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                raise BridgeError("bridge call failed before HQ answered") from exc
        finally:
            connection.close()
        if len(answer) > MAX_BRIDGE_BODY:
            raise BridgeError("bridge returned too much data")
        if response.status != http.client.OK:
            raise BridgeError("bridge failed: " + _detail(answer, response.status))
        media = (response.getheader("Content-Type") or "").partition(";")[0].strip()
        if media != "application/json":
            raise BridgeError("bridge answer does not match the contract: not JSON")
        try:
            decoded = json.loads(answer)
        except ValueError as exc:
            raise BridgeError("bridge answer does not match the contract: not JSON") from exc
        if not isinstance(decoded, dict):
            raise BridgeError("bridge answer does not match the contract: not an object")
        return decoded

    def claim(
        self, controller_id: str, *, capabilities: Sequence[str], lease_seconds: int, attempt: str
    ) -> dict[str, Any]:
        return self.call(
            "claim",
            {
                "controller-id": controller_id,
                "lease-seconds": lease_seconds,
                "capability": list(capabilities),
                "attempt": attempt,
            },
        )

    def renew(self, controller_id: str, operation: str, *, lease_seconds: int) -> dict[str, Any]:
        return self.call(
            "renew", {"controller-id": controller_id, "operation": operation, "lease-seconds": lease_seconds}
        )

    def report(self, controller_id: str, operation: str, report: Mapping[str, Any]) -> dict[str, Any]:
        return self.call("report", {"controller-id": controller_id, "operation": operation}, report)


def _detail(answer: bytes, status: int) -> str:
    try:
        detail = json.loads(answer).get("detail")
    except ValueError, AttributeError:
        detail = None
    if not isinstance(detail, str) or not detail:
        detail = http.client.responses.get(status, str(status))
    return detail if len(detail) <= _DETAIL_LIMIT else detail[: _DETAIL_LIMIT - 1] + "…"
