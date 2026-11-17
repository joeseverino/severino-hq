"""The open problems about one thing, for that thing's own page.

Every queue item names its subject, a link to that thing's page, so the problems
about a machine, service, record, domain or connection are the queue's items
whose subject is that page, as the queue's own cards; nothing is detected here.
Only the host's sections are read: their queue is stored until the estate
changes, while an extension's is composed per request about its own records.
Each answer is the reader's, through ``attention.readable_by``.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .derivations import derivation
from .derived_inputs import QUEUE_READS, estate_variant
from .first_seen import look_due, note_open
lazy from . import domains
lazy from .attention import readable_by
lazy from .dashboard import gathered_rows
lazy from .findings import finding_key
lazy from .projection import projection_scope
lazy from .security import Principal


def _host_entries() -> tuple[dict[str, Any], ...]:
    """The host's own queue, from the one derivation that composes it."""

    return domains.host_attention()


@derivation("attention.by_subject", reads=QUEUE_READS, vary=estate_variant)
def _by_subject() -> dict[str, tuple[dict[str, Any], ...]]:
    """The host's open items by the page each is about, in the queue's order."""

    found: dict[str, list[dict[str, Any]]] = {}
    for item in gathered_rows(_host_entries()):
        if item["notice"] or not (item["subject"] or {}).get("url"):
            continue
        found.setdefault(item["subject"]["url"], []).append(item)
    return {url: tuple(items) for url, items in found.items()}


def problems_about(url: str, principal: Principal) -> tuple[dict[str, Any], ...]:
    """The open problems whose subject is the page at ``url`` that ``principal`` may read, worst first.

    ``url`` is the page as ``entity_link`` addresses it, so a caller passes the
    link it already shows for the thing.
    """

    return tuple(readable_by(principal, list(_by_subject().get(url, ())))) if url else ()


@dataclass(frozen=True, slots=True)
class About:
    """What a part of a page is about: the pages of its things (``urls``), the
    keys of findings about a part with no page, such as a controller
    (``keys``), and the links an item about it sits at or below, such as a
    repository's (``under``)."""

    urls: Iterable[str] = ()
    keys: Iterable[str] = ()
    under: Iterable[str] = ()
    # The things among its subject's many the part is about, such as a
    # container on its machine and the image it runs: an item about another
    # of them is not its.
    parts: Iterable[str] = ()


@dataclass(frozen=True, slots=True)
class _Part:
    """One part of a page, as ``problems_by_part`` matches a queue item against it."""

    name: str
    pages: frozenset[str]
    keys: frozenset[str]
    roots: tuple[str, ...]
    parts: frozenset[str]

    @classmethod
    def of(cls, name: str, about: About) -> _Part:
        return cls(
            name,
            frozenset(filter(None, about.urls)),
            frozenset(filter(None, about.keys)),
            tuple(root.rstrip("/") for root in about.under if root),
            frozenset(filter(None, about.parts)),
        )

    def holds(self, entry: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
        """Whether the queue's ``item``, drawn from ``entry``, is about this part."""

        finding = entry["finding"]
        if self.parts and finding.part and finding.part not in self.parts:
            return False
        link = str(item["url"] or "")
        return (
            (item["subject"] or {}).get("url") in self.pages
            or finding_key(finding) in self.keys
            or any(link == root or link.startswith(f"{root}/") for root in self.roots)
        )


def problems_by_part(parts: Mapping[str, About], principal: Principal) -> tuple[tuple[str, dict[str, Any]], ...]:
    """The open problems ``principal`` may read about any of several parts of
    a page, each with the first part it is about, in the queue's order."""

    tests = [_Part.of(name, about) for name, about in parts.items()]
    # A held request is about no part of a page: it is decided on its own.
    kept = [entry for entry in _host_entries() if not entry["finding"].held]
    entries = list(zip(kept, gathered_rows(kept), strict=True))
    told = {id(item) for item in readable_by(principal, [item for _, item in entries])}
    found = []
    for entry, item in entries:
        if item["notice"] or id(item) not in told:
            continue
        name = next((test.name for test in tests if test.holds(entry, item)), None)
        if name is not None:
            found.append((name, item))
    return tuple(found)


def problem_counts(principal: Principal) -> dict[str, int]:
    """How many problems ``principal`` may read are open about each page that has any."""

    counts = {url: len(readable_by(principal, list(items))) for url, items in _by_subject().items()}
    return {url: count for url, count in counts.items() if count}


def note_open_problems() -> bool:
    """Take a look at what is open, when one is due; whether one was taken.

    Called where a report was just stored. A report that finds no look due
    costs one read.
    """

    if not look_due():
        return False
    with projection_scope():
        note_open(tuple(finding_key(entry["finding"]) for entry in _host_entries()))
    return True
