"""How HQ spells and compares DNS names. No Django, no registry: every layer imports it."""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
from typing import Any

# One DNS name that could answer: letters, digits and hyphens per label, no
# wildcard, no metadata label.
_HOSTNAME = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")


def normalized_hostname(name: Any) -> str:
    """One spelling of a DNS name: lowercase, trimmed, no trailing dot.

    A name is the join between every surface HQ has, so every comparison of
    names goes through this.
    """

    return str(name or "").strip().lower().rstrip(".")


def in_zone(name: Any, zone: Any) -> bool:
    """Whether a name, wildcard or not, is the zone or under it, on label boundaries."""

    bare = normalized_hostname(name).removeprefix("*.")
    wanted = normalized_hostname(zone)
    return bool(wanted) and (bare == wanted or bare.endswith(f".{wanted}"))


def is_hostname(name: Any) -> bool:
    """Whether a name, once normalized, is one host something could answer at."""

    text = normalized_hostname(name)
    return len(text) <= 253 and bool(_HOSTNAME.fullmatch(text))


def certificate_covers(domain: str, names: AbstractSet[str]) -> bool:
    """Whether a set of certificate names, wildcards included, answers for one name.

    One implementation of TLS wildcard matching: a wildcard covers one label.
    """

    normalized = normalized_hostname(domain)
    if normalized in names:
        return True
    _, separator, parent = normalized.partition(".")
    return bool(separator and f"*.{parent}" in names)
