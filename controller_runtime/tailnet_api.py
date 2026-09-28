"""The Tailscale API as the controller calls it.

The token for a connection, reads whose refusals name the missing scope, and
the device list.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
    ProviderError,
)
from control_plane.provider_adapters.parts import refuse_part
from . import connection_env, provider_http


TAILNET_API = "https://api.tailscale.com/api/v2"


def tailnet_token(connection_ref: str) -> str:
    """Exchange once per sweep and retain no token beyond that snapshot.

    The token lasts an hour, but a process-wide cache adds expiry and revocation
    behavior to get wrong. One sweep needs it several times, so that sweep shares
    one exchange and drops the result when its snapshot closes. The client itself
    remains held by the vault rather than by this process.
    """

    prefix = connection_env.connection_prefix("tailscale", connection_ref)

    def exchange() -> str:
        client_id = provider_http.required(prefix, "CLIENT_ID")
        client_secret = provider_http.required(prefix, "CLIENT_SECRET")
        body = urllib.parse.urlencode(
            {"client_id": client_id, "client_secret": client_secret}
        ).encode()
        try:
            with provider_http.open_url(
                f"{TAILNET_API}/oauth/token",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
                timeout=30,
            ) as response:
                payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise ValueError("OAuth response is not an object")
                token = payload.get("access_token", "")
        except urllib.error.HTTPError as exc:
            provider_http.release(exc)
            reason = (
                f"Tailscale refused the credential for {connection_ref} "
                f"({exc.code}). It has to be an OAuth client, not an API key."
            )
            raise ProviderError(
                reason, refusal=CREDENTIAL_REFUSAL, reason=reason
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderError("Tailscale did not answer the token request.") from exc
        if not token:
            raise ProviderError("Tailscale returned no access token.")
        return token

    return provider_http.snapshot_value(("tailscale-token", prefix), exchange)


def _tailnet_get(token: str, path: str) -> dict[str, Any]:
    """One tailnet-level read. Raises ProviderError with the reason."""

    try:
        with provider_http.open_url(
            f"{TAILNET_API}/tailnet/-/{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as response:
            found = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http.release(exc)
        raise ProviderError(f"/{path} answered HTTP {exc.code}.") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        provider_http.release(exc)
        raise ProviderError(f"/{path} could not be read: {type(exc).__name__}.") from None
    if not isinstance(found, dict):
        raise ProviderError(f"/{path} did not answer with an object.")
    return found


def tailnet_parts(token: str, parts: dict[str, tuple[str, ...]]) -> dict:
    """Several tailnet reads for one record, each a declared part: what was
    read. A part refused is reported through ``refuse_part``."""

    read: dict[str, dict[str, Any]] = {}
    for name, paths in parts.items():
        merged: dict[str, Any] = {}
        for path in paths:
            try:
                merged.update(_tailnet_get(token, path))
            except ProviderError as exc:
                refuse_part(name, exc)
                break
        read[name] = merged
    return read


def tailnet_api_devices(token: str) -> list[dict[str, Any]]:
    """Every device as the coordination server lists it. Raises when refused."""

    try:
        with provider_http.open_url(
            f"{TAILNET_API}/tailnet/-/devices?fields=all",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as response:
            found = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http.release(exc)
        raise tailnet_refused(
            "the tailnet device list", "devices:core:read", exc.code
        ) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        provider_http.release(exc)
        raise ProviderError(f"The tailnet device list could not be read: {type(exc).__name__}.") from None
    if not isinstance(found, dict):
        raise ProviderError("The tailnet device list did not answer with an object.")
    return [device for device in found.get("devices") or () if isinstance(device, dict)]


def tailnet_refused(what: str, scope: str, status: int) -> ProviderError:
    """One refused tailnet read, classified.

    The token was just exchanged, so the client itself is valid: Tailscale
    answers 403, and 404 on some endpoints, to a client without the scope. A
    401 is the token refused.
    """

    if status in (403, 404):
        return ProviderError(
            f"Tailscale refused {what} ({status}). The credential needs the "
            f"{scope} scope.",
            refusal=PERMISSION_REFUSAL,
        )
    if status == 401:
        return ProviderError(
            f"Tailscale refused {what} ({status}).",
            refusal=CREDENTIAL_REFUSAL,
            reason=f"Tailscale refused the access token ({status}).",
        )
    return ProviderError(f"Tailscale refused {what} ({status}).")


def tailnet_read(path: str, what: str, scope: str) -> dict[str, Any]:
    """One tailnet-level read. A refusal raises and names the scope it needs."""

    token = tailnet_token("")
    try:
        with provider_http.open_url(
            f"{TAILNET_API}/tailnet/-/{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        ) as response:
            found = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http.release(exc)
        raise tailnet_refused(f"the {what} read", scope, exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"Tailscale did not return readable {what}.") from exc
    if not isinstance(found, dict):
        raise ProviderError(f"Tailscale did not return readable {what}.")
    return found
