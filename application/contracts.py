"""Small shared validators for HQ's declarative registries."""

import re
from urllib.parse import urlsplit


DOTTED_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
DJANGO_ROUTE = re.compile(r"(?:[A-Za-z_][\w]*:)*[A-Za-z_][\w]*\Z")
SCOPE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
EFFECTS = frozenset(
    {"read", "remote_write", "destructive", "infrastructure_change"}
)


def endpoint_has_userinfo(endpoint: str) -> bool:
    """Whether an endpoint contains credential-like URL authority fields."""

    candidate = endpoint if "://" in endpoint else f"//{endpoint}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None
