"""The contact inbox, as the controller last read it.

The public site's contact form writes each message to a Cloudflare D1 table.
HQ holds no credential for it: the controller reads the table through the
cloudflare_api connection (the ``cloudflare.contact_submission`` reading) and
applies HQ's changes (the ``cloudflare.contact_inbox`` declaration). Every
function here reads what is stored and nothing else, so no page waits on D1.

A change HQ has asked for and the reading does not show yet is laid over the
row and marked pending, so a page shows what was asked for and that it is not
done.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.conf import settings

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.observations.cloudflare import CONTACT_SUBMISSION_KIND
from hq.domains.control_plane.provider_adapters.cloudflare import CONTACT_INBOX_KIND
lazy from hq.platform.application.projection import read_once

READING = CONTACT_SUBMISSION_KIND
DECLARATION = CONTACT_INBOX_KIND
DECLARATION_KEY = "contact-inbox"
RECENT_LIMIT = 4
PREVIEW_LENGTH = 160
# The fields a review changes, beside the status.
REVIEWED = ("status", "assigned_to", "admin_notes")

NOT_READ = "not_read"
OK = "ok"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Database:
    connection_ref: str
    account_id: str
    database_id: str
    name: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class Inbox:
    # NOT_READ: no reading yet. OK: read. UNAVAILABLE: the last read failed,
    # or the reading names databases HQ cannot choose between.
    state: str
    rows: tuple[dict[str, Any], ...] = ()
    problem: str = ""
    observed_at: datetime | None = None
    database: Database | None = None
    # The ids the reading holds, before HQ's changes are laid over.
    held: frozenset[int] = frozenset()
    # Changes waiting for the controller, and why it last refused them.
    waiting: int = 0
    refused: str = ""
    counts: dict[str, int] = field(default_factory=dict)


def _stored() -> ProviderInventory | None:
    return ProviderInventory.objects.filter(kind=READING, connected=True).first()


def declaration() -> ManagedResource | None:
    return ManagedResource.objects.filter(kind=DECLARATION, key=DECLARATION_KEY).first()


def _database_of(record: dict[str, Any]) -> Database:
    return Database(
        str(record.get("connection_ref", "")),
        str(record.get("account_id", "")),
        str(record.get("database_id", "")),
        str(record.get("database", "")),
    )


def choose_database(records: list[dict[str, Any]]) -> tuple[Database | None, str]:
    """The database the inbox is, from the databases the reading found the table in.

    The only one, or the one ``CLOUDFLARE_D1_DATABASE_NAME`` names. More than
    one is a problem to state, never a choice to make.
    """

    named = str(getattr(settings, "CLOUDFLARE_D1_DATABASE_NAME", "") or "").strip()
    found = {_database_of(record) for record in records}
    if named and found:
        found = {database for database in found if database.name == named}
        if not found:
            return None, f"No database named {named!r} holds contact messages."
    if len(found) > 1:
        names = ", ".join(sorted(database.name for database in found))
        return None, f"Several databases hold contact messages ({names}). Set CLOUDFLARE_D1_DATABASE_NAME."
    return next(iter(found), None), ""


def _differs(row: dict[str, Any], review: dict[str, Any]) -> bool:
    return any(str(row.get(name) or "") != str(review.get(name) or "") for name in REVIEWED)


def _pending(row: dict[str, Any] | None, review: dict[str, Any] | None) -> bool:
    """Whether the reading still holds a row the review differs from."""

    return row is not None and review is not None and _differs(row, review)


def _laid_over(rows: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    reviews = {int(review["id"]): review for review in spec.get("reviews") or ()}
    deleted = {int(identifier) for identifier in spec.get("deletions") or ()}
    shown = []
    for row in rows:
        identifier = int(row["id"])
        if identifier in deleted:
            continue
        review = reviews.get(identifier)
        waiting = _pending(row, review)
        if waiting and review is not None:
            row = {**row, **{name: review.get(name, "") for name in REVIEWED}}
        shown.append({**row, "pending": waiting, "message_preview": str(row.get("message", ""))[:PREVIEW_LENGTH]})
    return shown


def _waiting(rows: list[dict[str, Any]], spec: dict[str, Any]) -> int:
    by_id = {int(row["id"]): row for row in rows}
    reviews = sum(_pending(by_id.get(int(review["id"])), review) for review in spec.get("reviews") or ())
    deletions = sum(int(identifier) in by_id for identifier in spec.get("deletions") or ())
    return reviews + deletions


def _refusal(resource: ManagedResource | None) -> str:
    if resource is None or resource.generation == resource.observed_generation:
        return ""
    return next(
        (
            str(item.get("message", ""))
            for item in resource.conditions or ()
            if item.get("status") is True and item.get("type") == "Degraded"
        ),
        "",
    )


def current() -> Inbox:
    """The inbox as stored: its rows newest first, with HQ's changes laid over.

    Read once per projection: the header count, the dashboard card and the
    queue all ask, and must not disagree.
    """

    return read_once("contacts.inbox", _current)


def _current() -> Inbox:
    stored = _stored()
    if stored is None:
        return Inbox(NOT_READ)
    records = [record for record in stored.records or () if isinstance(record, dict) and record.get("id")]
    database, problem = choose_database(records)
    if problem:
        return Inbox(UNAVAILABLE, problem=problem, observed_at=stored.observed_at)
    own = [record for record in records if _database_of(record) == database]
    resource = declaration()
    spec = resource.spec if resource is not None and _database_of(resource.spec) == database else {}
    rows = _laid_over(own, spec)
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.get("status", ""))] = counts.get(str(row.get("status", "")), 0) + 1
    return Inbox(
        OK if stored.reachable else UNAVAILABLE,
        rows=tuple(rows),
        problem="" if stored.reachable else stored.error,
        observed_at=stored.observed_at,
        database=database,
        held=frozenset(int(record["id"]) for record in own),
        waiting=_waiting(own, spec),
        refused=_refusal(resource) if spec else "",
        counts=counts,
    )


def unread() -> tuple[int, str]:
    """The unread count and whether the last read reached D1. No network."""

    inbox = current()
    if inbox.state == NOT_READ:
        return 0, "unknown"
    return inbox.counts.get("unread", 0), "ok" if inbox.state == OK else "unavailable"


def recent() -> list[dict[str, Any]]:
    """The newest messages, as last read. No network."""

    return list(current().rows[:RECENT_LIMIT])


def search(q: str, limit: int) -> list[dict[str, Any]]:
    """Stored messages whose sender's name contains ``q``. No network.

    Only the name: the site-wide search lists a message by who sent it, and
    the Messages page searches addresses and message text.
    """

    wanted = q.strip().casefold()
    if not wanted:
        return []
    return [row for row in current().rows if wanted in str(row.get("name", "")).casefold()][:limit]
