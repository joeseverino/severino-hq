"""The example estate's pages as its server draws them, so dev:look can name a
record's page by its route or by a template it draws, and refuse a record's
address the estate does not hold before anything is looked at.

The estate is built on the first question, in a transaction rolled back once
the names are read, and asked as dev:look's browser asks it: as production,
marked as a prefetch so a page that would act is refused. Its pages are each
navigation page and every record's page those, and the parts they fetch into
themselves, link to, read in that order and only as far as a question needs,
and past them a record's page of each route those and the pages they lead
to link to, for a template none of those draws;
a page's templates are the test client's record of what
it rendered. The component catalog's entries are drawn as its gate draws
them, each only once a question reaches it, their templates heard as Django's
test rendering announces each.
"""

import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from functools import partial
from html import unescape
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.test.signals import template_rendered
from django.urls import Resolver404, resolve

from hq.platform.core.tests.browser.estate import fragments_of, navigation, owner_client
from hq.platform.core.tests.task import Drawn
lazy from hq.platform.core.example_estate import REQUEST

# Imported only once a template is looked for past the estate's links.
lazy from hq.platform.core.tests.browser import catalog

_HREF = re.compile(r'\shref="([^"#]+)')


class EstatePages:
    """:class:`task.Records` over a seed's estate, built when first asked and rolled back on close."""

    def __init__(self, seed: str):
        self.seed = seed
        self._stack = ExitStack()
        self._client: Any = None
        self._linked: list[str] = []
        self._crawl = self._crawling()
        self._at: dict[str, tuple[Drawn, list[str]] | None] = {}
        self._entries: dict[str, Drawn] = {}
        self._followed: list[str] = []
        self._follow = self._following()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self._stack.close()

    def _ask(self, address: str, **headers: str) -> Any:
        if self._client is None:
            self._client = self._stack.enter_context(owner_client(self.seed))
        return self._client.get(address, secure=True, follow=True, HTTP_SEC_PURPOSE="prefetch", **REQUEST, **headers)

    def _draw(self, address: str) -> tuple[Drawn, list[str]] | None:
        """What the page ``address`` lands on drew, and the links it and its parts hold."""

        if address not in self._at:
            self._at[address] = None
            response = self._ask(address)
            if response.status_code == 200 and "html" in response.get("Content-Type", ""):
                landed = response.request["PATH_INFO"]
                templates = _templates(response)

                def ask(_client: Any, url: str, **headers: str) -> Any:
                    part = self._ask(url, **headers)
                    templates.update(_templates(part))
                    return part

                body = response.content.decode()
                parts: dict[tuple[str, str], str] = {}
                fragments_of(self._client, landed, body, parts, ask)
                links = [
                    urljoin(landed, unescape(href)) for page in (body, *parts.values()) for href in _HREF.findall(page)
                ]
                self._at[address] = (Drawn(frozenset(templates)), links)
        return self._at[address]

    def at(self, address: str) -> Drawn | None:
        drawn = self._draw(address)
        return drawn[0] if drawn else None

    def entries(self) -> Iterator[tuple[str, Drawn]]:
        for name, entry in catalog.CATALOG.items():
            if name not in self._entries:
                self._entries[name] = Drawn(_rendered(partial(entry.page, name)))
            yield name, self._entries[name]

    def linked(self) -> Iterator[str]:
        # What an earlier question read first, then the rest as it is read.
        yield from list(self._linked)
        for address in self._crawl:
            if address not in self._linked:
                self._linked.append(address)
                yield address

    def followed(self) -> Iterator[str]:
        # What an earlier question followed first, then the rest as it is read.
        yield from list(self._followed)
        for address in self._follow:
            self._followed.append(address)
            yield address

    def _following(self) -> Iterator[str]:
        """Breadth first from the linked pages, one record's page of each route none of them is of:
        a route draws its records alike, and a route has no second page to lead on to, so the walk
        ends though a calendar's day links the next without end."""

        routes = {resolve(urlsplit(address).path).view_name for address in self.linked()}
        queue = list(self._linked)
        for address in queue:
            drawn = self._draw(address)
            for record in filter(None, map(_record_address, drawn[1] if drawn else ())):
                if (route := resolve(record).view_name) not in routes:
                    routes.add(route)
                    queue.append(record)
                    yield record

    def _crawling(self) -> Iterator[str]:
        """Each navigation page, then each record's page it links to: a record's page is drawn only when asked."""

        for address in navigation():
            yield address
            if page := self._draw(address):
                yield from dict.fromkeys(filter(None, map(_record_address, page[1])))


def _rendered(draw: Callable[[], object]) -> frozenset[str]:
    """Every template ``draw`` renders by name, as Django's test rendering announces each."""

    drawn: set[str] = set()

    def heard(sender: Any, template: Any, **_: Any) -> None:
        if template.name:
            drawn.add(template.name)

    template_rendered.connect(heard)
    try:
        draw()
    finally:
        template_rendered.disconnect(heard)
    return frozenset(drawn)


def _templates(response: Any) -> set[str]:
    return {template.name for template in getattr(response, "templates", ()) if template.name}


def _record_address(link: str) -> str | None:
    """``link``'s path where it is HQ's page of one record, else None."""

    parts = urlsplit(link)
    if parts.scheme or parts.netloc or not parts.path.startswith("/") or parts.path.startswith(settings.STATIC_URL):
        return None
    try:
        match = resolve(parts.path)
    except Resolver404:
        return None
    return parts.path if match.args or match.kwargs else None
