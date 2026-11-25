"""The image's server: HQ on Uvicorn's server, in this one process.

It answers on a probe socket from the start, which the image's HEALTHCHECK
asks, so Docker's health is this container's own even while another web
accepts on the same port. It serves HQ on a listener of its own where
``LISTENS_AT`` says, or, when ``HANDOFF`` names a socket, on the one
listener the host's supervisor hands it there.
"""

import asyncio
import contextlib
import functools
import logging
import os
import socket
import sys
from typing import Any, override

lazy import uvicorn
lazy from uvicorn.config import STARTUP_FAILURE

lazy from hq.platform.core import unix_server

# Where HQ's image serves, and what HQ knows of its own listener.
LISTENS_AT = ("127.0.0.1", 8000)

# How long each of HQ's servers lets a request in flight finish once it is
# asked to stop, before Uvicorn cancels it. Uvicorn otherwise waits for every
# open connection to close, and a listen stream stays open until its token
# expires. A page or a read answers well inside it; a stream or a tool call
# still running is cut, and its client reconnects or retries with its
# idempotency key. The host's stop (controller/host/layout.go StopGrace) is
# held above it.
GRACEFUL_SHUTDOWN_SECONDS = 5

APPLICATION = "hq.config.asgi:application"
# The image's directory for the web's own sockets, this account's alone
# (Dockerfile); the probe is in it, and the HEALTHCHECK names it.
PROBE_SOCKET = "/run/hq-web/ready.sock"
# The variable naming the socket a listener is handed on. Unset, the server
# binds LISTENS_AT itself.
HANDOFF = "SEVERINO_HANDED_LISTENER"
# The account a listener is taken from: root's supervisor, which holds the port.
HANDED_BY = 0

logger = logging.getLogger("severino.serve")


def config(application: Any = APPLICATION) -> uvicorn.Config:
    host, port = LISTENS_AT
    return uvicorn.Config(
        application,
        host=host,
        port=port,
        # Loopback only: every caller, /mcp/ included, comes through the
        # proxy in front. The application reads its forwarded headers from
        # SEVERINO_TRUSTED_PROXIES (hq/config/asgi.py), after recording the
        # peer they replace, so the server reads none of its own.
        proxy_headers=False,
        # The proxy speaks HTTP/1.1 upstream and holds a connection open for
        # 90 seconds; Uvicorn's default of 5 would close it under a request
        # that arrives later ("upstream prematurely closed connection while
        # reading response header"). Held above the proxy's, since whichever
        # side hangs up first decides, and it should be the one that knows a
        # request is not in flight.
        timeout_keep_alive=120,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


class ProbeProtocol(unix_server.PeerCheckedProtocol):
    """HTTP/1.1 for this account alone, answered as a caller on loopback is: it stands in for that listener."""

    @override
    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]  # uvicorn narrows the transport too
        super().connection_made(transport)
        self.client = (LISTENS_AT[0], 0)
        self.server = LISTENS_AT


def listening(sock: socket.socket) -> bool:
    """Whether ``sock`` listens, as the kernel says.

    macOS cannot say (its getsockopt has no SO_ACCEPTCONN), so a development
    run takes a bound socket with no peer, as only a listener is once handed.
    """

    # One branch per platform and no return inside either, so the type checker
    # skips the other platform's branch and reaches the one return.
    if sys.platform == "linux":
        listens = bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
    else:
        try:
            sock.getpeername()
        except OSError:
            listens = sock.getsockname()[1] != 0
        else:
            listens = False
    return listens


def handed(connection: socket.socket) -> socket.socket | None:
    """The one listener a root peer sent on ``connection``, when it is a listening TCP socket; else None."""

    _, fds, flags, _ = socket.recv_fds(connection, 1, 1)
    if len(fds) != 1 or flags & socket.MSG_CTRUNC:
        for fd in fds:
            os.close(fd)
        logger.warning("serve.handoff_refused fds=%d", len(fds))
        return None
    try:
        listener = socket.socket(fileno=fds[0])
    except OSError:
        os.close(fds[0])
        logger.warning("serve.handoff_refused not_a_socket")
        return None
    if listener.family != socket.AF_INET or listener.type != socket.SOCK_STREAM or not listening(listener):
        listener.close()
        logger.warning("serve.handoff_refused not_a_tcp_listener")
        return None
    return listener


async def readable(connection: socket.socket) -> None:
    loop = asyncio.get_running_loop()
    arrived = loop.create_future()
    loop.add_reader(connection.fileno(), arrived.set_result, None)
    try:
        await arrived
    finally:
        loop.remove_reader(connection.fileno())


class Server(uvicorn.Server):
    """Uvicorn's server with the probe beside its listener, and a handed listener in place of its own."""

    def __init__(self, config: uvicorn.Config, handoff: str = "") -> None:
        super().__init__(config)
        self.handoff = handoff
        self.waiting: asyncio.Task[None] | None = None

    @override
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        # Bound before the lifespan starts, so a path that cannot be served
        # safely stops the process before anything runs.
        probe, self.probe_bound = unix_server.private_listener(PROBE_SOCKET)
        handoff = unix_server.private_listener(self.handoff, backlog=1) if self.handoff else None
        # A list of no sockets is a server that binds none (Server.startup).
        await super().startup(sockets=[] if handoff else sockets)
        self.servers.append(await self.serving(probe, ProbeProtocol))
        if handoff is not None:
            self.waiting = asyncio.create_task(self.take(*handoff))
            self.waiting.add_done_callback(self.taken)

    async def serving(self, listener: socket.socket, protocol: type) -> asyncio.Server:
        """``listener`` served as Uvicorn serves its own, on this loop (Server.startup's create_protocol)."""

        config = self.config
        factory = functools.partial(
            protocol, config=config, server_state=self.server_state, app_state=self.lifespan.state
        )
        loop = asyncio.get_running_loop()
        return await loop.create_server(factory, sock=listener, ssl=config.ssl, backlog=config.backlog)

    async def take(self, waiting: socket.socket, bound: tuple[int, int]) -> None:
        """Serve the first listener handed on ``waiting``, then stop waiting.

        A peer the kernel does not name as root is dropped unheard. The
        connection closes once the listener is served, which is how the
        sender knows it was taken.
        """

        loop = asyncio.get_running_loop()
        try:
            while True:
                connection, _ = await loop.sock_accept(waiting)
                with connection:
                    peer = unix_server.peer_uid(connection)
                    if peer != HANDED_BY:
                        logger.warning("serve.handoff_refused uid=%s", "unknown" if peer is None else peer)
                        continue
                    await readable(connection)
                    try:
                        listener = handed(connection)
                    except OSError:
                        logger.warning("serve.handoff_refused unreadable", exc_info=True)
                        continue
                    if listener is None:
                        continue
                    self.servers.append(await self.serving(listener, self.config.http_protocol_class))
                    logger.info("serve.handed address=%s:%d", *listener.getsockname())
                    return
        finally:
            waiting.close()
            unix_server.remove_bound(self.handoff, bound)

    def taken(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("serve.handoff_failed", exc_info=task.exception())
            self.should_exit = True

    @override
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        if self.waiting is not None:
            self.waiting.cancel()
            # A wait that failed has said so (taken); this only ends it.
            await asyncio.wait([self.waiting])
        await super().shutdown(sockets=sockets)
        unix_server.remove_bound(PROBE_SOCKET, self.probe_bound)


def main() -> None:
    server = Server(config(), handoff=os.environ.get(HANDOFF, ""))
    # Ctrl-C ends the server as uvicorn's own command does, without a traceback.
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
