"""What an agent finds at /api/mcp/, read from HQ's MCP server the way a client reads it.

The SDK's own client asks the server in process, so the Agents page shows the
server's answers to its list requests, and whatever the server comes to offer
appears there with nothing here to change.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from asgiref.sync import async_to_sync
from django.conf import settings
from mcp import Client, types

from hq.platform.application.security import mcp_principal

from .identity import set_principal
from .security import metadata_url
from .server import mcp


@dataclass(frozen=True, slots=True)
class Offered:
    """One thing the server lists: its name, what it is called, and what it says of itself."""

    name: str
    title: str
    description: str
    # Only a tool says this, through its annotations.
    reads_only: bool = False

    @property
    def summary(self) -> str:
        """The description's first paragraph, which a row shows closed."""

        return next(iter(paragraphs(self.description)), "")

    @property
    def more(self) -> tuple[str, ...]:
        """The description's paragraphs after its summary, shown when the row is opened."""

        return paragraphs(self.description)[1:]


@dataclass(frozen=True, slots=True)
class Offer:
    """One of the server's lists, under the name the protocol gives it."""

    label: str
    items: tuple[Offered, ...]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where an agent connects and signs in, and what it finds there."""

    url: str
    sign_in: str
    protocol: str
    instructions: str
    offers: tuple[Offer, ...]

    @property
    def told(self) -> tuple[str, ...]:
        """The server's instructions, a paragraph each."""

        return paragraphs(self.instructions)


def paragraphs(text: str) -> tuple[str, ...]:
    """``text`` split at its blank lines, each paragraph's lines joined as they read."""

    return tuple(" ".join(part.split()) for part in re.split(r"\n\s*\n", text) if part.strip())


def _restates(name: str, title: str) -> bool:
    """A title that is only the name's words, which a row already shows."""

    def words(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    return words(title) == words(name)


# Each list the protocol defines: its name, the capability a server declares
# it under, the client's request and the field of the answer that holds it.
LISTS: tuple[tuple[str, str, str, str], ...] = (
    ("Tools", "tools", "list_tools", "tools"),
    ("Prompts", "prompts", "list_prompts", "prompts"),
    ("Resources", "resources", "list_resources", "resources"),
    ("Resource templates", "resources", "list_resource_templates", "resource_templates"),
)


def endpoint() -> Endpoint | None:
    """The MCP endpoint and what it lists, or None while no API resource is set and it is off."""

    resource = settings.SEVERINO_API_RESOURCE
    if not resource:
        return None
    protocol, instructions, offers = _ask()
    return Endpoint(
        url=urljoin(resource.rstrip("/") + "/", "mcp/"),
        sign_in=metadata_url(resource),
        protocol=protocol,
        instructions=instructions,
        offers=offers,
    )


def _ask() -> tuple[str, str, tuple[Offer, ...]]:
    # asgiref refuses to wait on a coroutine from a thread already running an
    # event loop, as a page answered inside a browser driver's handler is, so
    # there the question is asked from a thread of its own.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return async_to_sync(_listed)()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(async_to_sync(_listed)).result()


async def _listed() -> tuple[str, str, tuple[Offer, ...]]:
    # What a list holds can depend on the caller (resource templates do), so
    # the page shows what an agent holding the deployment's whole ceiling sees.
    with set_principal(mcp_principal()):
        async with Client(mcp.server) as client:
            declared = client.server_capabilities
            offers = []
            for label, capability, request, field in LISTS:
                if getattr(declared, capability, None) is None:
                    continue
                items = await _every(getattr(client, request), field)
                if items:
                    offers.append(Offer(label, tuple(_offered(item) for item in items)))
            return client.protocol_version, client.instructions or "", tuple(offers)


async def _every(request: Callable[..., Awaitable[Any]], field: str) -> list[Any]:
    """Every page of one list."""

    items: list[Any] = []
    cursor = None
    while True:
        page = await request(cursor=cursor)
        items.extend(getattr(page, field))
        cursor = page.next_cursor
        if not cursor:
            return items


def _offered(item: Any) -> Offered:
    # Only a tool carries annotations that name it and say whether it reads only.
    hints = item.annotations if isinstance(item, types.Tool) else None
    title = item.title or (hints.title if hints else None) or ""
    return Offered(
        name=item.name,
        title="" if _restates(item.name, title) else title,
        description=(item.description or "").strip(),
        reads_only=bool(hints and hints.read_only_hint),
    )
