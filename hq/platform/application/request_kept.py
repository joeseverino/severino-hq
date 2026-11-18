"""The request standard's kept request: one request that meets every check,
as plain data, and the same request forwarded by a trusted proxy. Each check's
examples (``request_standard``) are changes to it."""

from typing import Any

from django.utils.csp import CSP

from hq.platform.core.network import JUDGED, PROXY

from .reach import TAILNET
from .standards import Reading

# The devices the examples name, each an address of the tailnet's own range,
# so a check reads it as a tailnet device: the caller's laptop, a proxy, HQ's
# host, and an address no device holds.
LAPTOP, _PROXY_DEVICE, HQ_HOST, STRANGER = (str(TAILNET[0][host]) for host in (5, 7, 9, 77))

PERSON = {"username": "someone", "email": "someone@example.test", "tailnet_user": "someone@example.test"}

# A signed-in person's laptop on the tailnet reaching HQ's node over TLS,
# admitted by the policy and signed under tailnet lock, on an HQ set up as
# docs/Posture.md asks.
KEPT: dict[str, Any] = {
    "settings": {
        "SEVERINO_ENFORCE_TRUSTED_NETWORK": True,
        "AUTHENTICATION_BACKENDS": ["hq.platform.core.oidc.HQOIDCAuthenticationBackend"],
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_NAME": "__Host-sessionid",
        "SECURE_SSL_REDIRECT": True,
        "SECURE_HSTS_SECONDS": 31536000,
        "SECURE_CSP": {
            "script-src": [CSP.NONCE, CSP.STRICT_DYNAMIC],
            "frame-ancestors": [CSP.NONE],
            "require-trusted-types-for": ["'script'"],
        },
    },
    "address": LAPTOP,
    "channel": "tailnet",
    "host": "hq.example.test",
    "secure": True,
    "hops": (),
    "meta": {},
    "devices": {
        "a-laptop": {"user": "someone@example.test", "addresses": (LAPTOP,)},
        "hq-host": {"addresses": (HQ_HOST,), "observer": True, "reach": {443: ("someone@example.test",)}},
    },
    "caller": "a-laptop",
    "forwarder": "",
    "serves": "hq-host",
    "placed": True,
    "observer": "hq-host",
    "lock": {"enabled": True, "signers": [{"key": "tlpub:01"}]},
    "identity": PERSON,
    "edge": Reading.met("Tailnet ranges only"),
    "firewall": Reading.met("Tailnet interface only"),
}


def via_proxy(
    *,
    proxy_reach: tuple[str, ...] = ("someone@example.test",),
    hq_reach: tuple[str, ...] = ("tag:proxy",),
    real_ip: str = LAPTOP,
    scheme: str = "https",
) -> dict[str, Any]:
    """The kept request forwarded by a trusted proxy on the tailnet, which the
    policy lets the caller reach on 443 and lets reach HQ on 8000, with the
    headers its kind declares."""

    return {
        "hops": ((LAPTOP, JUDGED), (_PROXY_DEVICE, PROXY)),
        "forwarder": "a-proxy",
        "meta": {"SERVER_PORT": "8000", "HTTP_X_REAL_IP": real_ip, "HTTP_X_FORWARDED_SCHEME": scheme},
        "devices": {
            "a-proxy": {"tags": ("tag:proxy",), "addresses": (_PROXY_DEVICE,), "reach": {443: proxy_reach}},
            "hq-host": {"reach": {8000: hq_reach}},
        },
    }
