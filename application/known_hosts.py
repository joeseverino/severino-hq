"""Who actually runs a hostname, when the name itself says so.

Hostnames carry their operator in the domain they sit under:
``mx01.mail.icloud.com`` is iCloud, ``site.pages.dev`` is Cloudflare Pages,
``node.example.ts.net`` is Tailscale. Reading that off the name turns a
row of DNS trivia into the answer somebody was actually after -- who has the
mailbox, who serves the site.

Derived, not configured. Nothing here is a per-domain setting; it is a small
table of operators recognisable by the domain they publish under, and anything
unrecognised falls back to the registrable domain, which is already the useful
answer. The Email card and the Origin card both read it, because "who runs
this" is one question asked in two places.

Kept deliberately short. A directory of every hosting provider on the internet
would be a maintenance burden with no owner; this is the handful that appear in
one operator's own records, and being wrong here shows a slightly plainer label
on one card rather than breaking anything.
"""

from __future__ import annotations

# Suffix -> what a person calls it. Longest match wins, so a more specific
# suffix can name a service running under a broader one.
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

    An address is returned whole. Taking the last two labels of one produced
    "100.72" from 198.51.100.72 -- not a domain, not an address, and not
    anything a person could act on.
    """

    candidate = str(hostname).strip().lower().rstrip(".")
    if ":" in candidate or all(label.isdigit() for label in candidate.split(".")):
        return candidate
    labels = candidate.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else candidate


def operator(hostname: str) -> str:
    """What a person calls whoever runs this name, or its domain if unknown."""

    candidate = str(hostname).strip().lower().rstrip(".")
    matches = [
        (len(suffix), name)
        for suffix, name in OPERATORS
        if candidate == suffix or candidate.endswith(f".{suffix}")
    ]
    if matches:
        return max(matches)[1]
    return registrable(candidate)
