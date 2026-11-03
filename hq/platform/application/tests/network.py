"""The network as httpx2 and the resolver reach it, answered by a test.

A reader of an outside service is tested at the edge it really crosses: every
request httpx2 sends goes to the test's ``answer``, and every name resolves
from the test's table. Whatever the reader does between its caller and that
edge (building the request, following a redirect, reading the body, wording a
failure) runs as it does in production.
"""

import json
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import ip_address
from typing import Any
from unittest import TestCase, mock

import httpx2

from .. import reach

# A documentation address standing for one on the internet, inside `resolving`.
PUBLIC = "203.0.113.10"


class Response(httpx2.Response):
    """An HTTP answer: a JSON body (or ``content``, its bytes), its headers
    and its status."""

    def __init__(
        self,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        *,
        status: int = 200,
        content: bytes | None = None,
    ) -> None:
        super().__init__(status, headers=headers, content=json.dumps(body).encode() if content is None else content)


def moved(location: str, status: int = 302) -> Response:
    """A redirect to ``location``."""

    return Response(status=status, headers={"Location": location}, content=b"")


Answer = Callable[[httpx2.Request], httpx2.Response]


@contextmanager
def answering(answer: Answer) -> Iterator[None]:
    """Every request httpx2 sends, a redirect's included, is answered by
    ``answer(request)``, which returns a ``Response`` or raises what the
    network would (``httpx2.ConnectError``, ``httpx2.ReadTimeout``)."""

    def sent(_transport, request):
        return answer(request)

    with mock.patch.object(httpx2.HTTPTransport, "handle_request", sent):
        yield


def local_origin(test: TestCase, answer: Callable[[BaseHTTPRequestHandler], None]) -> str:
    """An http origin on the loopback whose every GET is ``answer(handler)``,
    stopped when ``test`` ends: the wire itself, for what a stand-in for the
    client could not show."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            answer(self)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    # A short poll, so stopping the server at cleanup does not wait out the default half second.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


@contextmanager
def resolving(names: Mapping[str, str]) -> Iterator[None]:
    """Each name in ``names`` resolves to its address, an address to itself,
    and any other name to nothing. The documentation ranges count as the
    internet here, so ``PUBLIC`` can stand for a public address."""

    def getaddrinfo(host, port, *_args, **_kwargs):
        address = names.get(host, host)
        try:
            found = ip_address(address)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        family = socket.AF_INET6 if found.version == 6 else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    with mock.patch("socket.getaddrinfo", getaddrinfo), mock.patch.object(reach, "DOCUMENTATION", ()):
        yield
