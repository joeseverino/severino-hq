"""Serve an ASGI application on a private Unix socket for a synchronous test, and call it."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from hq.platform.core.unix_server import serving


@contextlib.contextmanager
def socket_directory() -> Iterator[Path]:
    """A directory only this account can enter, short enough for a socket path."""

    directory = Path(tempfile.mkdtemp(prefix="hqb"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextlib.contextmanager
def served(application: Any, path: Path) -> Iterator[None]:
    """``serving`` on an event loop of its own, for the length of the block."""

    ready = threading.Event()
    state: dict[str, Any] = {}

    async def main() -> None:
        state["loop"] = asyncio.get_running_loop()
        state["stop"] = asyncio.Event()
        try:
            async with serving(application, str(path)):
                ready.set()
                await state["stop"].wait()
        except BaseException as exc:  # noqa: BLE001 - handed to the test's thread
            state["failure"] = exc
        finally:
            ready.set()

    thread = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
    thread.start()
    ready.wait(30)
    if "failure" in state:
        thread.join()
        raise state["failure"]
    try:
        yield
    finally:
        state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(30)


class UnixConnection(http.client.HTTPConnection):
    """HTTP over the Unix socket at ``path``."""

    def __init__(self, path: Path, timeout: float = 30) -> None:
        super().__init__("bridge", timeout=timeout)
        self.path = str(path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def post(path: Path, target: str, body: bytes = b"") -> tuple[int, bytes]:
    """One POST on a connection of its own: the status and the body."""

    connection = UnixConnection(path)
    try:
        connection.request("POST", target, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()
