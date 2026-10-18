"""What a page is made of, and what would fill it while it is empty.

A view names the things its page shows (``page_reads``) by the names they are
declared under: a resource kind, a reading, or a connection provider for data
that is neither. Which connection reads each is in that thing's own
declaration, so an empty page's remedy ("Connect Portainer") and the order the
Connections page offers services in both follow from those names alone, and
neither is written per page.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.urls import resolve

from .routes import reverse, routed


@dataclass(frozen=True, slots=True)
class Remedy:
    """What would fill an empty page: one line, a link when there is a page for it."""

    label: str
    url: str = ""


def readers(names: Iterable[str]) -> tuple[str, ...]:
    """The connection providers that read these things, in the order named."""

    from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
    from hq.domains.control_plane.observations import OBSERVATIONS
    from hq.domains.control_plane.provider_adapters import DECLARATIONS

    by_kind = {definition.kind: definition.connection_providers for definition in DECLARATIONS}
    found: list[str] = []
    for name in names:
        reading = OBSERVATIONS.get(name)
        found += [name, *by_kind.get(name, ()), *((reading.provider,) if reading else ())]
    return tuple(provider for provider in dict.fromkeys(found) if provider in CONNECTION_KINDS)


def setup_url(provider: str) -> str:
    """The Connections page, opened at how to connect ``provider``."""

    return f"{reverse('control_plane:connections')}?setup={provider}#setup-{provider}"


def remedy(names: Iterable[str]) -> Remedy | None:
    """The connection that would fill a page made of ``names``.

    The first that is already connected, at its row, since its state there says
    why nothing arrived; else how to connect the first that reads them.
    """

    from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
    from hq.domains.control_plane.models import ProviderConnection

    from .entity_links import NODE_KINDS

    providers = readers(names)
    if not providers:
        return None
    connected = dict(
        ProviderConnection.objects.filter(provider__in=providers)
        .order_by("connection_ref")
        .values_list("provider", "connection_ref")
    )
    page = NODE_KINDS["connection"].page
    for provider in providers:
        if provider in connected and page is not None:
            return Remedy(f"{CONNECTION_LABELS[provider]} connection", page(connected[provider]))
    return Remedy(f"Connect {CONNECTION_LABELS[providers[0]]}", setup_url(providers[0]))


def pages_reading() -> tuple[Any, ...]:
    """Every routed page that names what it reads, once each."""

    return tuple(dict.fromkeys(route.view for route in routed() if getattr(route.view, "page_reads", ())))


def need() -> dict[str, tuple[bool, int]]:
    """For each provider: whether the dashboard reads through it, and how many pages do."""

    found = resolve(reverse("dashboard")).func
    dashboard = getattr(found, "view_class", found)
    first = set(readers(getattr(dashboard, "page_reads", ())))
    counts: dict[str, int] = {}
    for view in pages_reading():
        for provider in readers(view.page_reads):
            counts[provider] = counts.get(provider, 0) + 1
    return {provider: (provider in first, count) for provider, count in counts.items()}
