"""A GitHub App as a connection: tokens minted per call, narrowed per call.

The app's private key never reaches this process. It is rendered beside the
controller's SSH identities, and ``ProviderRuntime.sign`` hands it to openssl,
which returns only the signature over GitHub's JWT. Every installation token is
minted for the repositories and permissions of one call and lasts an hour, so
nothing here holds a credential broader or longer-lived than the call it makes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import CREDENTIAL_REFUSAL, ProviderError, ProviderRuntime

PROVIDER = "github_app"
API = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub refuses a JWT that expires more than ten minutes out, and one issued in
# the future by a skewed clock; backdating a minute absorbs the skew.
_JWT_BACKDATE = 60
_JWT_LIFETIME = 540


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def repository(name: str) -> tuple[str, str]:
    """``(owner, repository)`` from ``owner/repository``, or a ProviderError."""

    owner, _, repo = str(name or "").strip().partition("/")
    if not owner or not repo or "/" in repo:
        raise ProviderError(f"{name!r} is not an owner/repository name.")
    return owner, repo


def app_id(runtime: ProviderRuntime, connection_ref: str = "") -> str:
    prefix = runtime.connection_prefix(PROVIDER, connection_ref)
    value = runtime.required(prefix, "APP_ID").strip()
    if not value.isdigit():
        raise ProviderError("The GitHub App's app_id is not a number.")
    return value


def _ref(runtime: ProviderRuntime, connection_ref: str) -> str:
    prefix = runtime.connection_prefix(PROVIDER, connection_ref)
    return runtime.required(prefix, "CONNECTION_REF")


def app_jwt(runtime: ProviderRuntime, connection_ref: str = "") -> str:
    """A ten-minute JWT naming the app, signed without this process seeing the key."""

    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(
        json.dumps(
            {
                "iat": now - _JWT_BACKDATE,
                "exp": now + _JWT_LIFETIME,
                "iss": app_id(runtime, connection_ref),
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}"
    signature = runtime.sign(_ref(runtime, connection_ref), signing_input.encode())
    return f"{signing_input}.{_b64(signature)}"


def _headers(token: str, scheme: str = "Bearer") -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"{scheme} {token}",
        "X-GitHub-Api-Version": API_VERSION,
    }


def as_app(
    runtime: ProviderRuntime,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    connection_ref: str = "",
) -> Any:
    """One request authenticated as the app itself."""

    return runtime.request(
        f"{API}{path}",
        method=method,
        headers=_headers(app_jwt(runtime, connection_ref)),
        payload=payload,
    )


def installation(runtime: ProviderRuntime, owner: str, repo: str, connection_ref: str = "") -> int:
    """The installation that covers one repository, asked of GitHub once per pass."""

    def load() -> int:
        found = as_app(
            runtime,
            f"/repos/{quote(owner)}/{quote(repo)}/installation",
            connection_ref=connection_ref,
        )
        if not isinstance(found, Mapping) or not isinstance(found.get("id"), int):
            raise ProviderError(f"The GitHub App is not installed on {owner}/{repo}.")
        return found["id"]

    return runtime.snapshot_value(("github_app.installation", connection_ref, owner, repo), load)


def token(
    runtime: ProviderRuntime,
    repositories: Iterable[str],
    permissions: Mapping[str, str],
    connection_ref: str = "",
) -> str:
    """An hour-long token for exactly these repositories and permissions.

    All repositories share an owner: an installation is one account, and a token
    spanning two would be two installations' worth of authority in one string.
    """

    names = sorted({str(name) for name in repositories})
    if not names or not permissions:
        raise ProviderError("A token names its repositories and its permissions.")
    owners = {repository(name)[0] for name in names}
    if len(owners) != 1:
        raise ProviderError("One token covers one account's repositories.")
    owner = owners.pop()
    grant = dict(sorted(permissions.items()))

    def load() -> str:
        first = repository(names[0])[1]
        installed = installation(runtime, owner, first, connection_ref)
        answer = as_app(
            runtime,
            f"/app/installations/{installed}/access_tokens",
            method="POST",
            payload={
                "repositories": [repository(name)[1] for name in names],
                "permissions": grant,
            },
            connection_ref=connection_ref,
        )
        value = answer.get("token") if isinstance(answer, Mapping) else None
        if not isinstance(value, str) or not value:
            raise ProviderError("GitHub did not issue an installation token.")
        return value

    key = ("github_app.token", connection_ref, tuple(names), tuple(grant.items()))
    return runtime.snapshot_value(key, load)


def call(
    runtime: ProviderRuntime,
    path: str,
    *,
    repositories: Iterable[str],
    permissions: Mapping[str, str],
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    connection_ref: str = "",
) -> Any:
    """One API call under a token minted for it alone."""

    minted = token(runtime, repositories, permissions, connection_ref)
    return runtime.request(
        f"{API}{path}", method=method, headers=_headers(minted), payload=payload
    )


def quote(part: str) -> str:
    return urllib.parse.quote(str(part), safe="")


def fingerprint(public_key: str) -> str:
    """The key's fingerprint as GitHub lists it: SHA256 of the DER public key."""

    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_ssh_public_key(public_key.strip().encode())
    except (ValueError, TypeError) as exc:
        raise ProviderError("The GitHub App's public key could not be read.") from exc
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def probe(runtime: ProviderRuntime, connection_ref: str) -> dict[str, Any]:
    """Whether the key still signs for the app, and which accounts it is installed on."""

    app = as_app(runtime, "/app", connection_ref=connection_ref)
    if not isinstance(app, Mapping) or not app.get("slug"):
        raise ProviderError(
            "GitHub did not accept the app's signature.", refusal=CREDENTIAL_REFUSAL
        )
    installations = as_app(runtime, "/app/installations", connection_ref=connection_ref)
    accounts = sorted(
        str((item.get("account") or {}).get("login", ""))
        for item in installations or ()
        if isinstance(item, Mapping)
    )
    key = fingerprint(runtime.signing_public_key(connection_ref))
    return {
        "detail": f"GitHub App {app['slug']}, key {key}",
        "reaches": [account for account in accounts if account],
    }
