"""The network as urllib and the resolver reach it, answered by a test.

A reader of an outside service is tested at the edge it really crosses: every
request urllib opens goes to the test's ``answer``, and every name resolves
from the test's table. Whatever the reader does between its caller and that
edge (building the request, reading the body, wording a failure) runs as it
does in production.
"""

import io
import json
import socket
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from email.message import Message
from ipaddress import ip_address
from typing import Any
from unittest import mock

from .. import reach

# A documentation address standing for one on the internet, inside `resolving`.
PUBLIC = "203.0.113.10"


class Response(io.BytesIO):
    """An HTTP answer as urllib hands it back: a JSON body and its headers."""

    def __init__(self, body: Any, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(json.dumps(body).encode())
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


Answer = Callable[[urllib.request.Request, float | None], Any]


@contextmanager
def answering(answer: Answer) -> Iterator[None]:
    """Every request urllib opens, by ``urlopen`` or an opener of its own, is
    answered by ``answer(request, timeout)``, which returns a ``Response`` or
    raises what the network would (``HTTPError``, ``URLError``)."""

    def opened(_director, request, data=None, timeout=None):
        if isinstance(request, str):
            request = urllib.request.Request(request, data)
        return answer(request, timeout)

    with mock.patch.object(urllib.request.OpenerDirector, "open", opened):
        yield


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
