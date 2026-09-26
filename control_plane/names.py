"""How HQ spells and compares DNS names. No Django, no registry: every layer imports it."""

from __future__ import annotations

from typing import Any


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
