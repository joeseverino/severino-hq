"""A tailscaled for tests: present on its socket, answering whois from a table.

Never a socket. The answers are shaped as the daemon writes them
(``apitype.WhoIsResponse`` in tailscale/tailscale, client/tailscale/apitype),
so the read of them is what the fake proves.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import override_settings

from hq.platform.application import tailscaled

OWNER = "owner@example.com"
# Where the host says the socket is; nothing here connects to it.
SOCKET = "/run/tailscale/tailscaled.sock"


def peer(name: str, node_id: str, *, user: str = OWNER, os: str = "macOS", tags: tuple[str, ...] = ()) -> dict:
    """One whois answer, as tailscaled writes it for a peer."""

    return {
        "Node": {
            "ID": 12345,
            "StableID": node_id,
            "Name": f"{name}.tailc0ffee.ts.net.",
            "ComputedName": name,
            "User": 1,
            "Key": "nodekey:0000000000000000000000000000000000000000000000000000000000000000",
            "Hostinfo": {"OS": os, "Hostname": name},
            "Tags": list(tags),
        },
        "UserProfile": {"ID": 1, "LoginName": "tagged-devices" if tags else user, "DisplayName": "Owner"},
        "CapMap": {},
    }


@contextmanager
def answering(peers: Mapping[str, dict] | None = None, *, failing: str = "") -> Iterator[mock.Mock]:
    """tailscaled on its socket, answering whois for each address in ``peers``
    and 404 for any other; ``failing`` makes every ask fail with that error.
    Yields the socket read, so a test can say it was never asked."""

    known = dict(peers or {})

    def get(path: str) -> tuple[int, bytes]:
        if failing:
            raise ConnectionRefusedError(failing)
        (address,) = parse_qs(urlsplit(path).query)["addr"]
        if address in known:
            return 200, json.dumps(known[address]).encode()
        return 404, b"peer not found\n"

    with (
        override_settings(SEVERINO_TAILSCALED_SOCKET=SOCKET),
        mock.patch.object(tailscaled, "get", side_effect=get) as asked,
    ):
        yield asked
