"""
Cloudflare D1 HTTP API client.

The contact-form submissions live in a Cloudflare D1 database, written by the
jseverino.com Pages Function. This module is the read/write bridge: HQ never
stores submissions locally, it queries D1 over the REST API on demand.

Uses only the standard library so HQ gains no new dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from django.conf import settings


class D1Error(RuntimeError):
    """Raised when D1 is unconfigured, unreachable, or returns an error."""


def _endpoint() -> str:
    account = getattr(settings, "CLOUDFLARE_ACCOUNT_ID", "")
    database = getattr(settings, "CLOUDFLARE_D1_DATABASE_ID", "")
    if not account or not database:
        raise D1Error(
            "Cloudflare D1 is not configured — set CLOUDFLARE_ACCOUNT_ID and "
            "CLOUDFLARE_D1_DATABASE_ID."
        )
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{account}"
        f"/d1/database/{database}/query"
    )


def query(sql: str, params: list | None = None) -> list[dict]:
    """
    Run one SQL statement against D1 and return the result rows as dicts.

    Works for SELECT (rows returned) and UPDATE/INSERT (empty list returned).
    """
    token = getattr(settings, "CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise D1Error("Cloudflare D1 is not configured — set CLOUDFLARE_API_TOKEN.")

    body = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise D1Error(f"D1 API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise D1Error(f"Could not reach the D1 API: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise D1Error(f"Unexpected D1 API response: {exc}") from exc

    if not payload.get("success"):
        raise D1Error(f"D1 API error: {payload.get('errors')}")

    result = payload.get("result") or []
    if not result:
        return []
    return result[0].get("results", []) or []
