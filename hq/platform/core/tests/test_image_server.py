"""The image's server takes its listener only from root."""

import contextlib
import os
import socket
import threading
from unittest import mock

import uvicorn
from django.test import SimpleTestCase

from hq.config import serve

from .unix_serving import socket_directory


async def answering(scope, receive, send):
    if scope["type"] == "lifespan":
        while (await receive())["type"] != "lifespan.shutdown":
            await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.shutdown.complete"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


class ImageServerTests(SimpleTestCase):
    def test_a_listener_from_any_account_but_root_is_refused_unheard(self):
        directory = self.enterContext(socket_directory())
        handoff = directory / "listener.sock"
        for patched in (
            mock.patch.object(uvicorn.Config, "configure_logging"),
            mock.patch.object(serve, "LISTENS_AT", ("127.0.0.1", 0)),
            mock.patch.object(serve, "PROBE_SOCKET", str(directory / "ready.sock")),
            mock.patch.object(serve, "HANDED_BY", os.geteuid() + 1),
        ):
            self.enterContext(patched)
        listener = self.enterContext(socket.create_server(("127.0.0.1", 0)))
        server = serve.Server(serve.config(answering), handoff=str(handoff))
        started = threading.Event()
        startup = server.startup

        async def starting(sockets=None):
            try:
                await startup(sockets=sockets)
            finally:
                started.set()

        server.startup = starting
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(setattr, server, "should_exit", True)
        self.assertTrue(started.wait(30), "the server never started")

        with (
            self.assertLogs("severino.serve", "WARNING") as logs,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender,
        ):
            sender.settimeout(30)
            sender.connect(str(handoff))
            # Refused unheard, the connection may close before it sends.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                socket.send_fds(sender, [b"x"], [listener.fileno()])
                sender.recv(1)

        self.assertIn("handoff_refused", logs.output[0])
        self.assertFalse([sock for each in server.servers for sock in each.sockets if sock.family == socket.AF_INET])
