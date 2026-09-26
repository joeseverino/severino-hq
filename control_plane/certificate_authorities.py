"""Certificate authorities by the codes providers report them under.

One registry, so every page that shows who issued a certificate names the
authority the same way. A code not listed is shown as reported.
"""

from __future__ import annotations

from types import MappingProxyType

AUTHORITIES = MappingProxyType(
    {
        "digicert": "DigiCert",
        "google": "Google Trust Services",
        "lets_encrypt": "Let's Encrypt",
        "sectigo": "Sectigo",
        "ssl_com": "SSL.com",
    }
)


def authority_name(code: object) -> str:
    """The display name for an authority code; an unknown code unchanged."""

    text = str(code or "").strip()
    return AUTHORITIES.get(text.lower(), text)
