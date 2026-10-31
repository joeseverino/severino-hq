"""Who actually runs a hostname, when the name itself says so.

A hostname carries its operator in the domain it sits under
(``mx01.mail.icloud.com`` is iCloud, ``site.pages.dev`` is Cloudflare Pages),
which turns a row of DNS trivia into who has the mailbox or serves the site.
The Email and Origin cards both ask it. Derived, never configured: a name no
operator matches is named by its registrable domain.
"""

from hq.domains.control_plane.names import in_zone, normalized_hostname

# Suffix -> what a person calls it. Longest match wins, so a more specific
# suffix can name a service running under a broader one. Only the handful in
# one operator's own records: a directory of every host would have no owner,
# and a miss costs a plainer label on one card, never a break.
OPERATORS: tuple[tuple[str, str], ...] = (
    ("icloud.com", "iCloud"),
    ("icloudmailadmin.com", "iCloud"),
    ("pages.dev", "Cloudflare Pages"),
    ("workers.dev", "Cloudflare Workers"),
    ("r2.dev", "Cloudflare R2"),
    ("ts.net", "Tailscale"),
    ("github.io", "GitHub Pages"),
    ("brevo.com", "Brevo"),
)


def registrable(hostname: str) -> str:
    """The last two labels of a name, which is the operator often enough.

    Deliberately not a public-suffix lookup: that needs a list which goes stale,
    and being wrong here costs a slightly odd label on one card.

    An address is returned whole: the last two labels of 198.51.100.72 are
    "100.72", which is neither a domain nor an address.
    """

    candidate = normalized_hostname(hostname)
    if ":" in candidate or all(label.isdigit() for label in candidate.split(".")):
        return candidate
    labels = candidate.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else candidate


def operator(hostname: str) -> str:
    """What a person calls whoever runs this name, or its domain if unknown."""

    candidate = normalized_hostname(hostname)
    matches = [(len(suffix), name) for suffix, name in OPERATORS if in_zone(candidate, suffix)]
    if matches:
        return max(matches)[1]
    return registrable(candidate)
