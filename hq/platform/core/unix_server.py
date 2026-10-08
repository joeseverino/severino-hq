"""An ASGI application served on a Unix socket only this account can reach.

The socket is the authorization. It lives in a directory this account owns and
nobody else can enter, it is this account's own with mode 0600, and every
connection is asked who made it: the kernel answers with the peer's uid
(``SO_PEERCRED``), and a peer that is not this account is dropped before a
byte of its request is read. Nothing here listens on a network address, and
the application it serves is given to no other listener.

Each rule fails closed. A directory another account can enter, a path that is
not this account's socket, or a platform that cannot name a peer stops the
listener; it never serves with less.
"""

import asyncio
import contextlib
import logging
import os
import socket
import stat
import struct
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, override

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

logger = logging.getLogger("severino.unix_server")

SOCKET_MODE = 0o600
# How long the server may take to start accepting before the caller is told it did not.
START_SECONDS = 10.0
# Calls in flight at once; the next is answered 503.
CONCURRENT_CALLS = 32

# <sys/un.h> on macOS, which the socket module does not export.
_DARWIN_SOL_LOCAL = 0
_DARWIN_LOCAL_PEERCRED = 1
# struct xucred: version, uid, then the group list.
_DARWIN_XUCRED = struct.Struct("IIh2x16I")
# struct ucred: pid, uid, gid.
_LINUX_UCRED = struct.Struct("3i")


class SocketRefused(RuntimeError):
    """The socket's path cannot be served safely."""


def peer_uid(connection: Any) -> int | None:
    """The uid of the process on the other end, as the kernel recorded it at connect.

    None when it cannot be read, which a caller treats as a stranger.
    """

    try:
        if sys.platform == "linux":
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _LINUX_UCRED.size)
            return int(_LINUX_UCRED.unpack(raw)[1])
        if sys.platform == "darwin":
            raw = connection.getsockopt(_DARWIN_SOL_LOCAL, _DARWIN_LOCAL_PEERCRED, _DARWIN_XUCRED.size)
            return int(_DARWIN_XUCRED.unpack(raw)[1])
    except OSError, struct.error:
        return None
    return None


def private_listener(path: str, *, backlog: int = 64) -> socket.socket:
    """Bind and listen on ``path``, or refuse.

    The directory must be this account's, a real directory, and closed to
    group and other. Whatever is at the path must be this account's own
    socket, left by a previous run; it is replaced. Anything else is refused.
    """

    if not Path(path).is_absolute() or os.path.normpath(path) != path:
        raise SocketRefused("The socket path is not an absolute, normal path.")
    directory = str(Path(path).parent)
    owner = os.geteuid()
    try:
        held = os.lstat(directory)
    except OSError as exc:
        raise SocketRefused("The socket's directory does not exist.") from exc
    if not stat.S_ISDIR(held.st_mode):
        raise SocketRefused("The socket's directory is not a directory.")
    if held.st_uid != owner:
        raise SocketRefused("The socket's directory belongs to another account.")
    if stat.S_IMODE(held.st_mode) & 0o077:
        raise SocketRefused("The socket's directory is open to another account.")
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != owner:
            raise SocketRefused("The socket path holds something that is not this account's socket.")
        Path(path).unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # bind refuses a path that exists, so nothing placed here in between is adopted.
        listener.bind(path)
        # Before listen: nothing can connect until the mode is the one served.
        Path(path).chmod(SOCKET_MODE)
        listener.listen(backlog)
    except OSError as exc:
        listener.close()
        raise SocketRefused("The socket could not be bound.") from exc
    listener.setblocking(False)
    return listener


class PeerCheckedProtocol(H11Protocol):
    """HTTP/1.1 for peers that are this account; anyone else is disconnected unheard."""

    @override
    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        peer = peer_uid(transport.get_extra_info("socket"))
        if peer != os.geteuid():
            logger.warning("unix_server.peer_refused uid=%s", "unknown" if peer is None else peer)
            transport.abort()


def _remove_socket(path: str) -> None:
    with contextlib.suppress(OSError):
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            Path(path).unlink()


@contextlib.asynccontextmanager
async def serving(application: Any, path: str) -> AsyncIterator[None]:
    """Serve ``application`` on the private socket at ``path`` for the life of the block.

    The server runs on a thread and an event loop of its own, so it shares
    neither a listener nor a routing table with whatever the caller serves.
    It is accepting when the block is entered, and the socket is gone when the
    block is left.
    """

    listener = private_listener(path)
    config = uvicorn.Config(
        application,
        http=PeerCheckedProtocol,
        ws="none",
        lifespan="off",
        log_config=None,
        limit_concurrency=CONCURRENT_CALLS,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, name="unix-server", daemon=True)
    thread.start()
    try:
        waited = 0.0
        while not server.started:
            if not thread.is_alive() or waited >= START_SECONDS:
                raise SocketRefused("The socket's server did not start.")
            await asyncio.sleep(0.01)
            waited += 0.01
        logger.info("unix_server.listening")
        yield
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join)
        _remove_socket(path)
