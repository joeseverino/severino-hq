"""Small shared validators and resolvers for HQ's declarative registries."""

import re
from urllib.parse import urlsplit

from django.urls import NoReverseMatch, reverse


DOTTED_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
DJANGO_ROUTE = re.compile(r"(?:[A-Za-z_][\w]*:)*[A-Za-z_][\w]*\Z")
SCOPE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
EFFECTS = frozenset(
    {"read", "remote_write", "destructive", "infrastructure_change"}
)


def route_url(route: str) -> str:
    """Resolve a route a registry declared, or "" when it does not resolve.

    A declared route is validated against ``DJANGO_ROUTE`` when its spec is
    built and reported by the startup system check when it cannot be reversed.
    Returning "" rather than raising keeps a page that merely *mentions* the
    route renderable if an unusual process skipped those checks: a broken link
    is a worse page, an exception is no page at all.
    """

    if not route:
        return ""
    try:
        return reverse(route)
    except NoReverseMatch:
        return ""


def endpoint_has_userinfo(endpoint: str) -> bool:
    """Whether an endpoint contains credential-like URL authority fields."""

    candidate = endpoint if "://" in endpoint else f"//{endpoint}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None
