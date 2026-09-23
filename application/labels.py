"""Human labels for stable machine names: capabilities, providers, kinds."""

from __future__ import annotations

_ACRONYMS = frozenset(
    {"acl", "api", "dns", "hq", "id", "mcp", "npm", "nws", "ssh", "tls", "url", "vin", "vm"}
)


def human_label(name: str) -> str:
    """``tls.certificate`` → "TLS Certificate", ``cloudflare_dns`` → "Cloudflare DNS"."""

    words = name.replace(".", " ").replace("_", " ").split()
    return " ".join(word.upper() if word in _ACRONYMS else word.title() for word in words)
