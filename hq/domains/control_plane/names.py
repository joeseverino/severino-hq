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


def names_a_host(name: str) -> bool:
    """Whether a DNS name could ever be something that answers.

    A label beginning with an underscore is reserved by RFC 8552 for metadata
    about a domain rather than for a host in it: ``_dmarc``, ``_domainkey``,
    ``_acme-challenge``, ``_sip._tcp``. Nothing is ever served there, and no
    name of that shape can be a service however it is published. The record
    type cannot tell: ``sig1._domainkey`` is a DKIM delegation published as a
    CNAME.
    """

    return not any(label.startswith("_") for label in str(name).split("."))


# A company's legal suffix, which says nothing about who it is.
_LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|gmbh|ag|sa|bv|plc|pty)\.?$",
    re.IGNORECASE,
)


def organisation_name(name: str) -> str:
    """How a person says a registry's organisation: "Example Registrar, Inc." is
    Example Registrar. Only a trailing legal suffix goes; the name itself is kept."""

    text = " ".join(str(name or "").split())
    trimmed = _LEGAL_SUFFIX.sub("", text).rstrip(" ,")
    return trimmed or text
