"""Small shared validators and resolvers for HQ's declarative registries."""

import re
from typing import override
from urllib.parse import urlsplit

from django.urls import NoReverseMatch
from django.utils.text import capfirst
from pydantic import BaseModel, ConfigDict

from hq.platform.application.effects import Effect
from hq.platform.application.reasons import declared_only
from hq.platform.application.routes import reverse

DOTTED_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
DJANGO_ROUTE = re.compile(r"(?:[A-Za-z_][\w-]*:)*[A-Za-z_][\w-]*\Z")
SCOPE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/&();-]*$")


def pascal(name: str) -> str:
    """``name``'s words run together, each capitalized: ``asset.create`` is ``AssetCreate``."""

    return "".join(capfirst(part) for part in re.split(r"[^0-9A-Za-z]+", name) if part)


def camel(name: str) -> str:
    """``name`` as ``pascal`` writes it, its first letter lowercased: ``asset.create`` is ``assetCreate``."""

    said = pascal(name)
    # An identifier, camel-cased, is no label.
    # ast-grep-ignore: first-letter-lowered-by-lower-first
    return said[:1].lower() + said[1:]


class Closed(BaseModel):
    """The base of every Pydantic model HQ reads input with that is not a
    command (a manifest, a query, a provider's spec): a field it does not
    declare is refused, never dropped. A command is a dataclass or TypedDict,
    which ``command_adapter`` closes. One whose validator says its own words
    does not load (``reasons.declared_only``)."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    @override
    def __pydantic_on_complete__(cls) -> None:
        declared_only(cls.__pydantic_core_schema__, f"Model {cls.__qualname__}")


def effect_glossary(field: str) -> str:
    """What each effect means, as a Markdown list introduced by the ``field`` it is read from."""

    meanings = "\n".join(f"- `{effect}`: {effect.meaning}" for effect in Effect)
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
