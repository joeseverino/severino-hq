"""Small shared validators and resolvers for HQ's declarative registries."""

import re
from urllib.parse import urlsplit

from django.urls import NoReverseMatch

from hq.platform.application.routes import reverse

DOTTED_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
DJANGO_ROUTE = re.compile(r"(?:[A-Za-z_][\w-]*:)*[A-Za-z_][\w-]*\Z")
SCOPE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
# What running a capability of each effect does, in the words every reference
# of the machine contract reads beside the name (the API document, MCP's
# capability catalog); a page says it shorter, as ``labels.EFFECT_LABELS``.
EFFECT_MEANINGS = {
    "read": "Reads and answers; changes nothing.",
    "remote_write": "Changes a record, in HQ or through a connection HQ holds.",
    "infrastructure_change": "Changes a live system HQ manages, such as a DNS record, a proxy route or a container.",
    "destructive": "Stops or deletes something; running it again does not bring it back.",
}
EFFECTS = frozenset(EFFECT_MEANINGS)


def effect_glossary(field: str) -> str:
    """What each effect means, as a Markdown list introduced by the ``field`` it is read from."""

    meanings = "\n".join(f"- `{effect}`: {meaning}" for effect, meaning in EFFECT_MEANINGS.items())
    return f"What `{field}` says running it does:\n\n{meanings}"


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


def endpoint_has_private_parts(endpoint: str) -> bool:
    """Whether display metadata carries a URL part that may contain a secret.

    Connection endpoints are identifiers, not request URLs. Query strings and
    fragments have no place in that contract and are common places for tokens,
    signatures and one-time credentials to hide. Rejecting the entire part is
    deterministic and safer than maintaining a list of parameter names that a
    provider can accidentally outgrow.
    """

    candidate = endpoint if "://" in endpoint else f"//{endpoint}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return True
    return bool(parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment)
