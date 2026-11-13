"""Certificate authorities by the codes providers report them under.

One registry, so every page that shows who issued a certificate names the
authority the same way. A code not listed is shown as reported.
"""

AUTHORITIES = frozendict(
    {
        "digicert": "DigiCert",
        "google": "Google Trust Services",
        "lets_encrypt": "Let's Encrypt",
        # Nginx Proxy Manager's code for a certificate it issued.
        "letsencrypt": "Let's Encrypt",
        "sectigo": "Sectigo",
        "ssl_com": "SSL.com",
    }
)

# Said in the issuer's place for a certificate uploaded to a proxy, which
# holds it without knowing who issued it.
UPLOADED = "uploaded"

# Nginx Proxy Manager's code for a certificate uploaded to it, by hand or by
# HQ's controller alike. Only NPM's: another provider's "other" is a name.
_NPM_UPLOADED = "other"


def authority_name(code: object) -> str:
    """The display name for an authority code; an unknown code unchanged."""

    text = str(code or "").strip()
    return AUTHORITIES.get(text.lower(), text)


def npm_issuer(code: object) -> str:
    """Who issued a certificate NPM holds, by NPM's provider code."""

    return UPLOADED if str(code or "").strip().lower() == _NPM_UPLOADED else authority_name(code)
