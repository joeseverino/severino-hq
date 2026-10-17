"""The open problems about one thing, for that thing's own page.

Every item of the queue says what it is about: its subject, a link to that
thing's page. So the problems about a machine, a service, a record, a domain
or a connection are the queue's items whose subject is that page, and a page
shows them as the same cards the queue does. Nothing is detected here.

Only the host's own sections are read. Their queue is a derived fact of the
estate, answered from one stored value until the estate changes; an
extension's items are composed on every request and are about its own records.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .derivations import derivation
from .derived_inputs import QUEUE_READS, estate_variant
from .first_seen import about, look_due, note_open


def _host_entries() -> tuple[dict[str, Any], ...]:
    """The host's own queue, from the one derivation that composes it."""

    from . import domains

    return domains._host_attention()


@derivation("attention.by_subject", reads=QUEUE_READS, vary=estate_variant)
def _by_subject() -> dict[str, tuple[dict[str, Any], ...]]:
    """The host's open items by the page each is about, in the queue's order."""

    from .dashboard import queue_item

    found: dict[str, list[dict[str, Any]]] = {}
    for entry in _host_entries():
        item = queue_item(entry["source_id"], entry["source"], entry["item"])
        if item["notice"] or not (item["subject"] or {}).get("url"):
            continue
        found.setdefault(item["subject"]["url"], []).append(item)
    return {url: tuple(items) for url, items in found.items()}


def problems_about(url: str) -> tuple[dict[str, Any], ...]:
    """The open problems whose subject is the page at ``url``, worst first.

    ``url`` is the page as ``entity_link`` addresses it, so a caller passes the
    link it already shows for the thing.
    """

    return _by_subject().get(url, ()) if url else ()


@dataclass(frozen=True, slots=True)
class About:
    """What a part of a page is about: the pages of its things (``urls``), the
    keys of findings about a part with no page, such as a controller
    (``keys``), and the links an item about it sits at or below, such as a
    repository's (``under``)."""

    urls: Iterable[str] = ()
    keys: Iterable[str] = ()
    under: Iterable[str] = ()


def problems_by_part(parts: Mapping[str, About]) -> tuple[tuple[str, dict[str, Any]], ...]:
    """The open problems about any of several parts of a page, each with the
    first part it is about, in the queue's order."""

    from .dashboard import queue_item

    tests = [
        (
            name,
            set(filter(None, about.urls)),
            set(filter(None, about.keys)),
            tuple(root.rstrip("/") for root in about.under if root),
        )
        for name, about in parts.items()
    ]
    found = []
    for entry in _host_entries():
        item = queue_item(entry["source_id"], entry["source"], entry["item"])
        if item["notice"]:
            continue
        page, key, link = (item["subject"] or {}).get("url"), getattr(entry["item"], "key", ""), str(item["url"] or "")
        for name, pages, keys, roots in tests:
            if page in pages or key in keys or any(link == root or link.startswith(f"{root}/") for root in roots):
                found.append((name, item))
                break
    return tuple(found)


def problem_counts() -> dict[str, int]:
    """How many problems are open about each page that has any."""

    return {url: len(items) for url, items in _by_subject().items()}


def note_open_problems() -> bool:
    """Take a look at what is open, when one is due; whether one was taken.

    Called where a report was just stored. A report that finds no look due
    costs one read.
    """

    from .projection import projection_scope

    if not look_due():
        return False
    with projection_scope():
        note_open(tuple(about(entry["item"]) for entry in _host_entries()))
    return True
