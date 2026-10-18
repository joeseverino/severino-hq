"""What a record's page lists as related to it, as `partials/_related.html` draws it.

Each group is a `references.Mentions`: a heading and its items, each a link and
a few words beside it. A page composes its groups with `related(...)`, which
keeps them in the order given and drops the empty ones, so every record page
draws its related records in one shape.
"""

from collections.abc import Iterable
from typing import Any

from .entity_links import EntityLink, entity_link
from .moments import when_day
from .money import money
from .references import Mention, Mentions


def related(*groups: Mentions) -> tuple[Mentions, ...]:
    """The groups in the order given, without the empty ones."""

    return tuple(group for group in groups if group.items)


def links(heading: str, found: Iterable[EntityLink]) -> Mentions:
    """Links a page already holds (a document's project, its source note)."""

    return Mentions(heading, tuple(Mention(link) for link in found))


def projects(rows: Iterable[Any], also: Iterable[EntityLink] = (), *, heading: str = "Projects") -> Mentions:
    """Projects by name, then any the record's documents name (``also``)."""

    return Mentions(
        heading,
        (
            *(Mention(entity_link("project", row.slug, label=row.name)) for row in rows),
            *(Mention(link) for link in also),
        ),
    )


def writeups(rows: Iterable[Any], *, status: bool = False) -> Mentions:
    """Writeups and pages by title; with ``status``, whether each is out."""

    return Mentions(
        "Writeups and pages",
        tuple(
            Mention(entity_link("writeup", row.slug, label=row.title), row.get_status_display() if status else "")
            for row in rows
        ),
    )


def assets(rows: Iterable[Any]) -> Mentions:
    """Assets by name, with what each cost where that was recorded."""

    return Mentions(
        "Assets",
        tuple(
            Mention(
                entity_link("asset", row.slug, label=row.item_name), money(row.total_cost) if row.total_cost else ""
            )
            for row in rows
        ),
    )


def expenses(rows: Iterable[Any]) -> Mentions:
    """Expenses by who was paid for what, with the day and the amount."""

    return Mentions(
        "Expenses",
        tuple(
            Mention(
                entity_link("expense", str(row.pk), label=row.label), f"{when_day(row.date)} · {money(row.total_cost)}"
            )
            for row in rows
        ),
    )


def receipts(rows: Iterable[Any]) -> Mentions:
    """Receipts by who issued them, with the amount."""

    return Mentions(
        "Receipts",
        tuple(Mention(entity_link("receipt", str(row.pk), label=row.label), money(row.amount)) for row in rows),
    )
