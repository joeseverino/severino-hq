"""Provider console pages, built from ids a record already stores.

Nothing here reads a secret or calls a provider. A record that lacks the id a
page needs gets no link, and its name renders without one.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

CLOUDFLARE_DASHBOARD = "https://dash.cloudflare.com"
CLOUDFLARE_ZERO_TRUST = "https://one.dash.cloudflare.com"
TAILSCALE_ADMIN = "https://login.tailscale.com/admin"


def _path(*parts: str) -> str:
    return "/".join(quote(part, safe="") for part in parts)


def _account_page(base: str, record: Mapping[str, Any], *path: str) -> str:
    account = str(record.get("account_id", "") or "").strip()
    if not account or not all(path):
        return ""
    return f"{base}/{_path(account, *path)}"


def cloudflare_dashboard(record: Mapping[str, Any], *path: str) -> str:
    """A page under the record's Cloudflare account, or "" without its account id."""

    return _account_page(CLOUDFLARE_DASHBOARD, record, *path)


def cloudflare_zero_trust(record: Mapping[str, Any], *path: str) -> str:
    """A Zero Trust page for the record's account, or "" without its account id."""

    return _account_page(CLOUDFLARE_ZERO_TRUST, record, *path)


def tailscale_machine(record: Mapping[str, Any]) -> str:
    """The admin console page for a device, addressed by its tailnet IPv4 address."""

    for value in record.get("addresses") or ():
        try:
            address = ipaddress.ip_address(str(value))
        except ValueError:
            continue
        if address.version == 4:
            return f"{TAILSCALE_ADMIN}/machines/{address}"
    return ""
