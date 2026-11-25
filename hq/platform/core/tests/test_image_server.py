"""The image's server: its probe from the start, and HQ on a listener of its own or the one root hands it."""

import contextlib
import http.client
import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import uvicorn
from django.test import SimpleTestCase

from hq.config import serve

from .unix_serving import UnixConnection, socket_directory


async def caller(scope, receive, send):
    """Answers with the address the server says called it."""

    if scope["type"] == "lifespan":
        while (await receive())["type"] != "lifespan.shutdown":
            await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.shutdown.complete"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": (scope["client"] or ("",))[0].encode()})


def answered(connection: http.client.HTTPConnection) -> tuple[int, bytes]:
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def tcp_ports(server) -> list[int]:
    return [sock.getsockname()[1] for each in server.servers for sock in each.sockets if sock.family == socket.AF_INET]


def hand(path: Path, *fds: int) -> None:
    """Send ``fds`` on the handoff socket and wait until the server closes the connection."""

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
        sender.settimeout(30)
        sender.connect(str(path))
        # A peer refused unheard may find the connection closed before it sends.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            socket.send_fds(sender, [b"x"], list(fds))
            sender.recv(1)


def tcp_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


class ImageServerTests(SimpleTestCase):
    def setUp(self):
        manager = socket_directory()
        self.directory = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.probe = self.directory / "ready.sock"
        self.handoff = self.directory / "listener.sock"
        for patched in (
            mock.patch.object(uvicorn.Config, "configure_logging"),
            mock.patch.object(serve, "LISTENS_AT", ("127.0.0.1", 0)),
            mock.patch.object(serve, "PROBE_SOCKET", str(self.probe)),
            mock.patch.object(serve, "HANDED_BY", os.geteuid()),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    @contextlib.contextmanager
    def running(self, handoff: str = "") -> Iterator[serve.Server]:
        server = serve.Server(serve.config(caller), handoff=handoff)
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
        self.assertTrue(started.wait(30), "the server never started")
        try:
            yield server
        finally:
            server.should_exit = True
            thread.join(30)

    def probed(self) -> tuple[int, bytes]:
        return answered(UnixConnection(self.probe))

    def test_on_its_own_it_serves_a_listener_of_its_own_and_the_probe_as_loopback(self):
        with self.running() as server:
            (port,) = tcp_ports(server)
            self.assertEqual(answered(http.client.HTTPConnection("127.0.0.1", port, timeout=30))[0], 200)
            self.assertEqual(self.probed(), (200, serve.LISTENS_AT[0].encode()))
        self.assertFalse(self.probe.exists())

    def test_handed_a_listener_it_serves_that_one_and_closes_only_its_own_copy(self):
        listener = tcp_listener()
        self.addCleanup(listener.close)
        with self.running(str(self.handoff)) as server:
            self.assertEqual(tcp_ports(server), [], "a server waiting for its listener bound one")
            self.assertEqual(self.probed()[0], 200)

            hand(self.handoff, listener.fileno())

            port = listener.getsockname()[1]
            self.assertEqual(answered(http.client.HTTPConnection("127.0.0.1", port, timeout=30))[0], 200)
            self.assertFalse(self.handoff.exists(), "the server still waits after taking its listener")
        # The sender's copy still queues a caller for whoever serves it next.
        socket.create_connection(("127.0.0.1", port), timeout=30).close()

    def test_only_one_listening_tcp_socket_is_taken(self):
        listener = tcp_listener()
        self.addCleanup(listener.close)
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix.bind(str(self.directory / "other.sock"))
        unix.listen()
        self.addCleanup(unix.close)
        unbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(unbound.close)
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        with self.running(str(self.handoff)) as server:
            for name, fds in (
                ("a unix listener", [unix.fileno()]),
                ("a tcp socket that does not listen", [unbound.fileno()]),
                ("not a socket", [read]),
                ("two listeners", [listener.fileno(), listener.fileno()]),
            ):
                with self.subTest(name), self.assertLogs("severino.serve", "WARNING") as logs:
                    hand(self.handoff, *fds)
                self.assertIn("handoff_refused", logs.output[0])
                self.assertEqual(tcp_ports(server), [])
                self.assertTrue(self.handoff.exists(), "a refused handoff stopped the wait")

            hand(self.handoff, listener.fileno())
            self.assertEqual(tcp_ports(server), [listener.getsockname()[1]])

    def test_a_listener_from_any_account_but_root_is_refused_unheard(self):
        listener = tcp_listener()
        self.addCleanup(listener.close)
        with (
            mock.patch.object(serve, "HANDED_BY", os.geteuid() + 1),
            self.running(str(self.handoff)) as server,
            self.assertLogs("severino.serve", "WARNING") as logs,
        ):
            hand(self.handoff, listener.fileno())
            self.assertEqual(tcp_ports(server), [])
        self.assertIn("handoff_refused", logs.output[0])
