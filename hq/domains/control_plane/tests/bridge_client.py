"""Call the bridge application from a test, as a request on its Unix socket.

The application runs under ``async_to_sync``, so an action's database work
happens on the test's own thread and sees the test's transaction. That
connection is the test's to close, so the application's connection hygiene is
set aside here, as Django's test client sets aside the request signals'. The
scope names a Unix listener, which is the only kind the application serves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

from asgiref.sync import async_to_sync

from hq.domains.control_plane.bridge_application import application

UNIX_LISTENER = ("/run/example/bridge.sock", None)


@dataclass(frozen=True)
class Answer:
    status: int
    media_type: str
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


def request(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: bytes = b"",
    method: str = "POST",
    server: tuple[str, int | None] | None = UNIX_LISTENER,
    app: Any = application,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Answer:
    """One raw request to the bridge application."""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query or {}, doseq=True).encode(),
        "headers": [(b"host", b"bridge"), *headers],
        "client": None,
        "server": server,
    }
    sent = [{"type": "http.request", "body": body, "more_body": False}]
    received: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return sent.pop(0) if sent else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        received.append(message)

    with patch("hq.domains.control_plane.bridge_application.close_old_connections"):
        async_to_sync(app)(scope, receive, send)
    start = next(message for message in received if message["type"] == "http.response.start")
    media_type = dict(start["headers"]).get(b"content-type", b"").decode()
    content = b"".join(m.get("body", b"") for m in received if m["type"] == "http.response.body")
    return Answer(start["status"], media_type, content)


def call(action: str, payload: Any = None, **query: Any) -> Any:
    """Run one action and return its answer, failing on a refusal.

    Keyword names are the contract's with underscores for hyphens.
    """

    answer = request(
        f"/{action}",
        query={name.replace("_", "-"): value for name, value in query.items()},
        body=json.dumps(payload).encode() if payload is not None else b"",
    )
    if answer.status != 200:
        raise AssertionError(f"bridge {action} answered {answer.status}: {answer.body.decode()}")
    return answer.json()
