"""The Cloudflare API as the controller calls it.

Credentials, token verification, the breaker that stops a refused token being
retried, and paged and cursor-paged reads.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    NETWORK_FAILURE,
    ProviderError,
    cloudflare_refusal,
)
from . import connection_env, provider_http


# ----- Cloudflare ------------------------------------------------------------
#
# Two credentials, each scoped to one surface.
#
# `cloudflare_dns` is deliberately narrow: it can read the zones on the account
# and read and write their DNS records, and nothing else. Zone settings,
# analytics and every account-level surface answer 403 to it. That is why the
# zone provider declares no reconcile it could perform (see the capability
# registry) and why nothing here reaches for a setting it cannot change.
#
# `cloudflare_api` carries the account surface (analytics, zone settings,
# registration) and reaches no DNS record. Neither is a subset of the other,
# so a provider states which one it needs and gets exactly that.


CLOUDFLARE_API_URL = "https://api.cloudflare.com/client/v4"

# List paging. The Go controller (providers/cloudflare_api.go) holds the same numbers.
CLOUDFLARE_PER_PAGE = 100
CLOUDFLARE_ACCOUNT_PER_PAGE = 50
CLOUDFLARE_MAX_PAGES = 50
CLOUDFLARE_MAX_CURSOR_PAGES = 200


def cloudflare_url(
    connection_ref: str = "", *, provider: str = "cloudflare_dns"
) -> str:
    """The API base; <PREFIX>_URL overrides the public one."""

    prefix = connection_env.connection_prefix(provider, connection_ref)
    return (os.environ.get(f"{prefix}_URL", "").strip() or CLOUDFLARE_API_URL).rstrip("/")


def cloudflare_token(
    connection_ref: str = "", *, provider: str = "cloudflare_dns"
) -> str:
    return provider_http.required(connection_env.connection_prefix(provider, connection_ref), "API_TOKEN")


def cloudflare_envelope(
    path: str,
    *,
    method: str = "GET",
    payload: Any = None,
    provider: str = "cloudflare_dns",
    connection_ref: str = "",
) -> dict[str, Any]:
    """One Cloudflare call, returning the whole envelope with its errors kept.

    Not routed through ``_request`` because Cloudflare says something useful in
    the body of a 400: "Content for A record must be a valid IPv4 address",
    "An identical record already exists", and the shared helper turns every
    non-200 into the same sentence. A rejected DNS change that only says
    "Provider request failed: HTTPError" is a support ticket to yourself.

    ``success`` is checked here rather than by each caller, because Cloudflare
    also answers 200 with ``success: false``: a token missing one permission
    returns no ``result`` at all, and a list helper reading ``result`` off that
    collects nothing and reports an empty estate. An account that looks empty
    and an account that refused to answer must not read the same.

    Both credentials come through here: the zone-scoped DNS token and the
    account-scoped analytics one differ only in which connection names them,
    which is what ``provider`` and ``connection_ref`` select.
    """

    prefix = connection_env.connection_prefix(provider, connection_ref)
    cloudflare_breaker(prefix)
    url = f"{cloudflare_url(connection_ref, provider=provider)}{path}"
    headers = {
        "Authorization": (
            f"Bearer {cloudflare_token(connection_ref, provider=provider)}"
        ),
        "Accept": "application/json",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        with provider_http.open_url(url, data=body, headers=headers, method=method) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            detail = cloudflare_errors(exc.read())
        raise cloudflare_refused(
            prefix,
            f"Cloudflare refused the request: {detail}",
            detail,
            status=exc.code,
            verified=lambda: cloudflare_verified(provider, connection_ref),
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(
            f"Cloudflare request failed: {type(exc).__name__}.",
            failure=NETWORK_FAILURE,
        ) from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ProviderError("Cloudflare returned invalid JSON.") from exc
    if not parsed.get("success", False):
        detail = cloudflare_errors(raw)
        raise cloudflare_refused(
            prefix, f"Cloudflare refused the request: {detail}", detail
        )
    return parsed if isinstance(parsed, dict) else {}


def _refused_credentials() -> dict[str, str]:
    """Credentials refused outright during this sweep, by connection prefix."""

    snapshot = provider_http.PROVIDER_SNAPSHOT.get()
    if snapshot is None:
        return {}
    return snapshot.setdefault(("refused-credentials",), {})


def cloudflare_breaker(prefix: str) -> None:
    """Raise without a call when this sweep has already seen the credential refused.

    Every further call with a refused credential is refused too, and repeated
    failures lock the token out, so each Cloudflare call consults this first.
    """

    refused = _refused_credentials()
    if prefix in refused:
        raise ProviderError(
            f"Cloudflare refused the request: {refused[prefix]} "
            "Not retried for the rest of this sweep.",
            refusal=CREDENTIAL_REFUSAL,
            reason=refused[prefix],
        )


def _cloudflare_verification(provider: str, connection_ref: str) -> dict[str, Any]:
    """``/user/tokens/verify``'s result for one credential, once per sweep.

    ``{}`` when it does not verify. Called directly rather than through
    ``cloudflare_envelope``, whose refusals consult this.
    """

    def verify() -> dict[str, Any]:
        url = f"{cloudflare_url(connection_ref, provider=provider)}/user/tokens/verify"
        headers = {
            "Authorization": (
                f"Bearer {cloudflare_token(connection_ref, provider=provider)}"
            ),
            "Accept": "application/json",
        }
        try:
            with provider_http.open_url(url, headers=headers) as response:
                parsed = json.loads(response.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            provider_http.release(exc)
            return {}
        result = parsed.get("result") if isinstance(parsed, dict) else None
        if not parsed.get("success") or not isinstance(result, dict):
            return {}
        return result

    prefix = connection_env.connection_prefix(provider, connection_ref)
    return provider_http.snapshot_value(("cloudflare-verification", prefix), verify)


def cloudflare_verified(provider: str, connection_ref: str) -> bool:
    """Whether the credential itself verifies as active."""

    return _cloudflare_verification(provider, connection_ref).get("status") == "active"


def cloudflare_refused(
    prefix: str,
    message: str,
    detail: str,
    *,
    status: int = 0,
    verified: Callable[[], bool] | None = None,
) -> ProviderError:
    """The error for one Cloudflare refusal, recording a refused credential."""

    refusal = cloudflare_refusal(detail, status=status, verified=verified)
    if refusal == CREDENTIAL_REFUSAL:
        _refused_credentials()[prefix] = detail
        return ProviderError(message, refusal=CREDENTIAL_REFUSAL, reason=detail)
    return ProviderError(message, refusal=refusal)


def cloudflare_request(
    path: str, *, method: str = "GET", payload: Any = None, connection_ref: str = ""
) -> Any:
    """The zone-scoped DNS surface, unwrapped to the result callers expect."""

    return cloudflare_envelope(
        path, method=method, payload=payload, connection_ref=connection_ref
    ).get("result")


def cloudflare_errors(raw: bytes) -> str:
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return "an unreadable error"
    messages = [
        str(error.get("message", "")).strip()
        for error in parsed.get("errors") or ()
        if str(error.get("message", "")).strip()
    ]
    return "; ".join(messages) or "no reason given"


def cloudflare_paged(path: str) -> list[dict[str, Any]]:
    """Every page of a DNS-surface list.

    A zone that outgrew one page would otherwise have its tail silently
    reported as absent, and "absent" is the word this system acts on, so the
    reconciler would set about recreating records that were there all along.
    """

    return cloudflare_list(path, per_page=CLOUDFLARE_PER_PAGE, provider="cloudflare_dns")


# Account readings through `cloudflare_api`. Every list is fetched once per
# sweep through the provider snapshot; per-item requests are made only where
# Cloudflare has no list that carries the field. The account allows 1,200
# requests per five minutes.


def cloudflare_api_refs() -> tuple[str, ...]:
    # No declared connection still reads once, so a missing credential raises.
    return connection_env.provider_connection_refs("cloudflare_api") or ("",)


def cloudflare_api_result(path: str, connection_ref: str) -> Any:
    return (cloudflare_api_request(path, connection_ref) or {}).get("result")


def cloudflare_api_zones(connection_ref: str) -> list[dict[str, Any]]:
    return provider_http.snapshot_value(
        ("cloudflare-api-zones", connection_ref),
        lambda: cloudflare_api_list("/zones", connection_ref, per_page=CLOUDFLARE_ACCOUNT_PER_PAGE),
    )


def cloudflare_api_request(path: str, connection_ref: str = "") -> Any:
    """One account-surface request through the account-scoped credential."""

    return cloudflare_envelope(
        path, provider="cloudflare_api", connection_ref=connection_ref
    )


def cloudflare_api_list(
    path: str, connection_ref: str = "", *, per_page: int = CLOUDFLARE_PER_PAGE
) -> list[dict[str, Any]]:
    """Every page from one Cloudflare account list endpoint."""

    return cloudflare_list(path, connection_ref, per_page=per_page, provider="cloudflare_api")


def _list_batch(response: dict[str, Any]) -> list[Any]:
    """A list page's result: missing or null is empty, anything but a list is invalid."""

    batch = response.get("result")
    if batch is None:
        return []
    if not isinstance(batch, list):
        raise ProviderError("Cloudflare list returned an invalid result.")
    return batch


def cloudflare_list(
    path: str,
    connection_ref: str = "",
    *,
    per_page: int = CLOUDFLARE_PER_PAGE,
    provider: str = "cloudflare_api",
) -> list[dict[str, Any]]:
    """The one pagination loop for page-numbered lists, on either credential.

    The account surface reports total_pages and it decides when present: an
    endpoint may cap per_page below what was asked, so a short page is not
    proof of the last one. The DNS surface answers with its result only, so a
    short page ends it. Non-object entries are dropped.
    """

    def fetch(page_path: str) -> dict[str, Any]:
        if provider == "cloudflare_api":
            return cloudflare_api_request(page_path, connection_ref) or {}
        extra = {"connection_ref": connection_ref} if connection_ref else {}
        return {"result": cloudflare_request(page_path, **extra)}

    collected: list[dict[str, Any]] = []
    for page in range(1, CLOUDFLARE_MAX_PAGES + 1):
        separator = "&" if "?" in path else "?"
        response = fetch(f"{path}{separator}per_page={per_page}&page={page}")
        batch = _list_batch(response)
        collected.extend(item for item in batch if isinstance(item, dict))
        total_pages = int((response.get("result_info") or {}).get("total_pages") or 0)
        if (page >= total_pages) if total_pages else (len(batch) < per_page):
            return collected
    raise ProviderError("Cloudflare list did not terminate.")


def cloudflare_api_cursor_list(
    path: str, connection_ref: str = "", *, per_page: int = CLOUDFLARE_ACCOUNT_PER_PAGE
) -> list[dict[str, Any]]:
    """Every page from a cursor-paginated Cloudflare list; an empty cursor ends it."""

    collected: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(CLOUDFLARE_MAX_CURSOR_PAGES):
        query = f"per_page={per_page}" + (f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else "")
        separator = "&" if "?" in path else "?"
        response = cloudflare_api_request(f"{path}{separator}{query}", connection_ref)
        batch = _list_batch(response or {})
        collected.extend(item for item in batch if isinstance(item, dict))
        cursor = str(((response or {}).get("result_info") or {}).get("cursor") or "")
        if not cursor:
            return collected
    raise ProviderError("Cloudflare list did not terminate.")
