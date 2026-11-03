"""What a secret the controller handles looks like, so none enters HQ through the bridge.

A report carries what a provider answered, and a provider can answer with the
credential HQ's controller sent it. The shapes are the credentials the
controller holds or mints (``controller/providers``), by the prefix or
structure each issuer documents.

A payload has each match struck in place, the way the controller's mask
strikes a secret it resolved, and goes on: some of its strings are written by
outsiders (a contact form's message, a pull request's title), and refusing
the request for one would let anyone stop a sync.
"""

import re
from typing import Any

# What stands where a secret was, the controller mask's own word.
REDACTED = "[redacted]"

# Each shape, by the words a strike is logged in. Anchored on a prefix or a
# structure no ordinary value has, with a body long enough that the prefix
# alone, written in prose, is not struck. Every match is longer than
# REDACTED, so a struck string never outgrows a length its schema allows.
_SHAPES = {
    # From its header to its footer, or to the end of a text cut short.
    "a private key": (
        r"(?:-----BEGIN [A-Z0-9 ]*)?PRIVATE KEY(?: BLOCK)?-----"
        r"(?s:.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|.*)"
    ),
    "a GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})",
    "a Tailscale key": r"\btskey-[A-Za-z0-9-]{16,}",
    "a Cloudflare token": r"\bcf(?:ut|at|k|oat)_[A-Za-z0-9_-]{32,}",
    "a 1Password service account token": r"\bops_[A-Za-z0-9+/=_-]{40,}",
    "a Portainer access token": r"\bptr_[A-Za-z0-9+/=]{40,}",
    "an age identity": r"AGE-SECRET-KEY-1[0-9A-Z]{50,}",
    # A JSON Web Token: a header and claims, each a base64url JSON object
    # (``eyJ`` is ``{"``), and a signature. A 1Password Connect token and
    # Nginx Proxy Manager's session token are both one.
    "a JSON Web Token": r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*",
}
_SHAPE = re.compile("|".join(f"(?P<s{index}>{pattern})" for index, pattern in enumerate(_SHAPES.values())))
_WORDS = tuple(_SHAPES)

# A member named for what it holds. The contract leaves a report's status and
# an inventory record open, so a credential with no shape of its own (a
# password, a legacy API token) is caught by the name it travels under.
_SECRET_NAMES = ("private", "secret", "token", "password", "credential")
NAMED = "a member named as a secret"

Path = tuple[str | int, ...]
# Where a secret was struck, as a JSON Pointer, and what it was.
Strike = tuple[str, str]


def _pointer(path: Path) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path) or "/"


def _named_secret(key: str, value: Any) -> bool:
    # A boolean or null holds no secret, so a flag such as a repository's
    # ``private`` is a fact about the thing, not the thing's credential.
    if value is None or isinstance(value, bool):
        return False
    lowered = key.lower()
    return any(name in lowered for name in _SECRET_NAMES)


def _text(path: Path, text: str, strikes: list[Strike]) -> str:
    def strike(found: re.Match[str]) -> str:
        strikes.append((_pointer(path), _WORDS[int(str(found.lastgroup).removeprefix("s"))]))
        return REDACTED

    return _SHAPE.sub(strike, text)


def _struck(path: Path, node: Any, strikes: list[Strike]) -> Any:
    if isinstance(node, str):
        return _text(path, node, strikes)
    if isinstance(node, list):
        return [_struck((*path, index), child, strikes) for index, child in enumerate(node)]
    if not isinstance(node, dict):
        return node
    kept: dict[str, Any] = {}
    for member, child in node.items():
        key = _text(path, str(member), strikes)
        here = (*path, key)
        if _named_secret(key, child):
            strikes.append((_pointer(here), NAMED))
            kept[key] = REDACTED
        else:
            kept[key] = _struck(here, child, strikes)
    return kept


def struck(value: Any) -> tuple[Any, list[Strike]]:
    """``value`` with every secret in it struck, and where each was.

    Every string is read, a member's name too. A member named as a secret has
    its value struck whole, unless it holds a boolean or null.
    """

    strikes: list[Strike] = []
    return _struck((), value, strikes), strikes
