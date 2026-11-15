"""Small shared validators and resolvers for HQ's declarative registries."""

import re
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit

from django.urls import NoReverseMatch
from pydantic import BaseModel, ConfigDict

from hq.platform.application.routes import reverse
from hq.platform.application.tone import Tone

DOTTED_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
DJANGO_ROUTE = re.compile(r"(?:[A-Za-z_][\w-]*:)*[A-Za-z_][\w-]*\Z")
SCOPE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


# A spec's field typed by it takes the value's string and holds the member, so
# an effect nothing declares fails where it is written.
class Effect(StrEnum):
    """What running a command does, least consequential first."""

    # The words a page says it in, the words every reference of the machine
    # contract reads beside the name (the API document, MCP's capability
    # catalog), and the tone a page says it in beside a command.
    label: str
    meaning: str
    tone: Tone

    READ = "read", "Reads only", "Reads and answers; changes nothing.", Tone.NEUTRAL
    REMOTE_WRITE = (
        "remote_write",
        "Changes HQ",
        "Changes a record, in HQ or through a connection HQ holds.",
        Tone.NEUTRAL,
    )
    INFRASTRUCTURE_CHANGE = (
        "infrastructure_change",
        "Changes a live system",
        "Changes a live system HQ manages, such as a DNS record, a proxy route or a container.",
        Tone.ATTENTION,
    )
    DESTRUCTIVE = (
        "destructive",
        "Stops or deletes",
        "Stops or deletes something; running it again does not bring it back.",
        Tone.SERIOUS,
    )

    # Defaulted so ``Effect("read")``, a lookup by value, reads as one to a type checker.
    def __new__(cls, value: str, label: str = "", meaning: str = "", tone: Tone = Tone.NEUTRAL) -> Self:
        member = str.__new__(cls, value)
        member._value_ = value
        member.label = label
        member.meaning = meaning
        member.tone = tone
        return member

    @property
    def rank(self) -> int:
        """Its place in the order, a read 0: a higher rank does more."""

        return list(Effect).index(self)

    @property
    def changes(self) -> bool:
        """Whether running it changes anything."""

        return self is not Effect.READ


class Closed(BaseModel):
    """The base of every Pydantic model HQ reads input with that is not a
    command (a manifest, a query, a provider's spec): a field it does not
    declare is refused, never dropped. A command is a dataclass or TypedDict,
    which ``command_adapter`` closes."""

    model_config = ConfigDict(extra="forbid")


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
