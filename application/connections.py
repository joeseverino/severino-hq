"""Controller-only provider connections and non-mutating authentication probes."""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ConnectionProbe:
    connection_ref: str
    provider: str
    ok: bool
    message: str


def _controller_ssl_context():
    ca_file = os.environ.get("SEVERINO_CONTROLLER_CA_FILE", "").strip()
    return ssl.create_default_context(cafile=ca_file or None)


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Controller environment is missing {name}.")
    return value


def _npm_probe(env: dict[str, str], open_url: Callable) -> ConnectionProbe:
    connection_ref = _required(env, "NPM_CONNECTION_REF")
    endpoint = _required(env, "NPM_URL").rstrip("/") + "/api/tokens"
    body = json.dumps(
        {
            "identity": _required(env, "NPM_USERNAME"),
            "secret": _required(env, "NPM_PASSWORD"),
        }
    ).encode()
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with open_url(
        request, timeout=10, context=_controller_ssl_context()
    ) as response:
        payload = json.load(response)
    if not payload.get("token"):
        raise ValueError("NPM authentication returned no token.")
    return ConnectionProbe(connection_ref, "npm", True, "Authentication succeeded.")


def _adguard_probe(env: dict[str, str], open_url: Callable) -> ConnectionProbe:
    connection_ref = _required(env, "ADGUARD_CONNECTION_REF")
    endpoint = _required(env, "ADGUARD_URL").rstrip("/") + "/control/status"
    credentials = (
        f"{_required(env, 'ADGUARD_USERNAME')}:"
        f"{_required(env, 'ADGUARD_PASSWORD')}"
    )
    authorization = base64.b64encode(credentials.encode()).decode()
    request = Request(
        endpoint,
        method="GET",
        headers={"Authorization": f"Basic {authorization}"},
    )
    with open_url(
        request, timeout=10, context=_controller_ssl_context()
    ) as response:
        payload = json.load(response)
    if "dns_addresses" not in payload:
        raise ValueError("AdGuard status response is not recognized.")
    return ConnectionProbe(
        connection_ref, "adguard", True, "Authentication succeeded."
    )


def _cloudflare_dns_probe(env: dict[str, str], open_url: Callable) -> ConnectionProbe:
    connection_ref = _required(env, "CLOUDFLARE_DNS_CONNECTION_REF")
    endpoint = _required(env, "CLOUDFLARE_DNS_URL").rstrip("/")
    headers = {
        "Authorization": f"Bearer {_required(env, 'CLOUDFLARE_DNS_API_TOKEN')}"
    }
    request = Request(
        f"{endpoint}/user/tokens/verify",
        method="GET",
        headers=headers,
    )
    with open_url(
        request, timeout=10, context=_controller_ssl_context()
    ) as response:
        payload = json.load(response)
    if not payload.get("success"):
        raise ValueError("Cloudflare DNS token verification failed.")
    return ConnectionProbe(
        connection_ref, "cloudflare_dns", True, "Authentication succeeded."
    )


def preflight_connections(
    env: dict[str, str] | None = None,
    *,
    open_url: Callable = urlopen,
) -> list[ConnectionProbe]:
    source = dict(os.environ if env is None else env)
    probes = []
    for provider, probe in (
        ("adguard", _adguard_probe),
        ("npm", _npm_probe),
        ("cloudflare_dns", _cloudflare_dns_probe),
    ):
        try:
            probes.append(probe(source, open_url))
        except (ValueError, HTTPError, URLError, TimeoutError) as exc:
            probes.append(
                ConnectionProbe(
                    source.get(f"{provider.upper()}_CONNECTION_REF", provider),
                    provider,
                    False,
                    str(exc),
                )
            )
    return probes


def main() -> int:
    probes = preflight_connections()
    payload = {
        "ok": all(probe.ok for probe in probes),
        "connections": [
            {
                "connection_ref": probe.connection_ref,
                "provider": probe.provider,
                "ok": probe.ok,
                "message": probe.message,
            }
            for probe in probes
        ],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
