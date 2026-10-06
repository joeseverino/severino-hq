"""Human labels for stable machine names: capabilities, providers, kinds."""

from __future__ import annotations

_ACRONYMS = frozenset(
    {"acl", "api", "dns", "hq", "id", "mcp", "npm", "nws", "ssh", "tls", "url", "vin", "vm"}
)


def human_label(name: str) -> str:
    """``tls.certificate`` → "TLS Certificate", ``cloudflare_dns`` → "Cloudflare DNS"."""

    words = name.replace(".", " ").replace("_", " ").split()
    return " ".join(word.upper() if word in _ACRONYMS else word.title() for word in words)


def lower_first(text: str) -> str:
    """A label as it reads mid-sentence, leaving an acronym alone.

    "Proxy host" belongs lowercase after "Add"; "TLS certificate" does not, and
    lowering its first letter would produce "tLS certificate". A word whose second
    letter is a capital ("HQ", "TLS", "IPv6") is an acronym and keeps its case.
    """

    if text[1:2].isupper():
        return text
    return text[:1].lower() + text[1:]


_SIBILANT_ENDINGS = ("s", "x", "z", "ch", "sh")


def plural(noun: str) -> str:
    """Many of a noun, as English spells it: "entries", "addresses", "hosts".

    The one place a plural is derived from a singular. A noun whose plural
    this cannot reach ("person") is given whole to ``counted`` instead, or
    declared as its model's ``verbose_name_plural``.
    """

    lowered = noun.lower()
    if lowered.endswith("y") and lowered[-2:-1] not in ("", *"aeiou"):
        return f"{noun[:-1]}ies"
    if lowered.endswith(_SIBILANT_ENDINGS):
        return f"{noun}es"
    return f"{noun}s"


def human_bytes(value: int | float) -> str:
    """A byte count as people say it: ``512 B``, ``38 MB``, ``1.7 GB``."""

    amount = max(0.0, float(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit in {"B", "KB", "MB"} else f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


# What running a command does, in the words every page uses for it: the search
# results, the command's own page and the agents table.
EFFECT_LABELS = {
    "read": "Reads only",
    "remote_write": "Changes HQ",
    "infrastructure_change": "Changes a live system",
    "destructive": "Deletes",
}


def effect_label(effect: str) -> str:
    return EFFECT_LABELS.get(effect, "")
