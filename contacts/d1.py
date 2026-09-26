"""
Cloudflare D1 HTTP API client.

The contact-form submissions live in a Cloudflare D1 database, written by the
example.com Pages Function. This module is the read/write bridge over the REST
API. HQ stores only the inbox state `contacts.inbox` keeps (count, and names
without email or message); everything else is read from D1 when asked for.

Uses only the standard library so HQ gains no new dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.request

from django.conf import settings

from application.ui import counted
from django.urls import reverse

from application.connection_contracts import (
    ConnectionAbility,
    ConnectionFact,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
)
from application.security import Capability
from core.errors import UpstreamUnavailable


class D1Error(UpstreamUnavailable):
    """Raised when D1 is unconfigured, unreachable, or returns an error."""


def connection_specs():
    """Emit D1's configured authority and its registered HQ processes."""

    def instances():
        token = getattr(settings, "CLOUDFLARE_API_TOKEN", "").strip()
        # One collection built and returned once: an unconfigured environment
        # differs in length, never in the shape a caller unpacks.
        emitted = []
        if not token:
            return tuple(emitted)
        try:
            target = database()
        except D1Error as exc:
            emitted.append(
                ConnectionInstance(
                    id="cloudflare-d1-contacts",
                    label="Cloudflare D1 contacts",
                    kind="cloudflare_d1",
                    status="attention",
                    status_label="database unknown",
                    detail=str(exc),
                    credential_model="scoped",
                )
            )
            return tuple(emitted)
        emitted.append(
            ConnectionInstance(
                id="cloudflare-d1-contacts",
                label="Cloudflare D1 contacts",
                kind="cloudflare_d1",
                status="good",
                status_label="configured",
                detail=(
                    "Account, database, and token known. Health is checked when an "
                    "operation runs."
                ),
                endpoint=(
                    f"https://api.cloudflare.com/client/v4/accounts/{target.account}"
                    f"/d1/database/{target.database}"
                ),
                # An API token is Cloudflare's scoped kind. HQ does not yet
                # read its permissions, so its abilities report a scoped
                # credential with the requirement unverified rather than a
                # proof nobody has checked.
                credential_model="scoped",
                ability_names=(
                    "cloudflare.d1_submissions_read",
                    "cloudflare.d1_submission_review",
                    "cloudflare.d1_submission_delete",
                ),
                targets=(
                    ConnectionLink("Contact submissions", reverse("contacts:list")),
                ),
                facts=(
                    ConnectionFact("Database", target.database),
                    ConnectionFact("Source", target.source),
                ),
            )
        )
        return tuple(emitted)

    return (
        ConnectionSpec(
            name="hq.cloudflare_d1",
            label="Cloudflare D1",
            summary="Cloudflare D1 databases HQ queries directly.",
            required_capability=Capability.MANAGE_CONTACTS,
            instance_provider=instances,
            abilities=(
                ConnectionAbility(
                    "cloudflare.d1_submissions_read",
                    "Read contact submissions",
                    "List and view contact submissions.",
                    capability="contact.submissions.list",
                    subject_resource="contact.submissions",
                ),
                ConnectionAbility(
                    "cloudflare.d1_submission_review",
                    "Review contact submission",
                    "Set status, assignee, and notes.",
                    effect="remote_write",
                    capability="contact.submission.review",
                    subject_resource="contact.submissions",
                ),
                ConnectionAbility(
                    "cloudflare.d1_submission_delete",
                    "Delete contact submission",
                    "Delete one contact submission after confirmation.",
                    effect="destructive",
                    capability="contact.submission.delete",
                    subject_resource="contact.submissions",
                ),
            ),
            web_route="contacts:list",
            management_route="contacts:list",
            documentation_url="https://developers.cloudflare.com/api/resources/d1/",
            secret_store="Deployment secrets",
        ),
    )


D1_KIND = "cloudflare.d1_database"


@dataclass(frozen=True)
class D1Target:
    account: str
    database: str
    # "settings", or the reading the missing part was derived from.
    source: str


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def database() -> D1Target:
    """The account and database HQ queries.

    Settings win. Whatever they leave out is derived from the stored
    ``cloudflare.d1_database`` reading: the database the settings name by id or
    by ``CLOUDFLARE_D1_DATABASE_NAME``, or the only one. More than one candidate
    is an error, never a choice.
    """

    account = _setting("CLOUDFLARE_ACCOUNT_ID")
    database_id = _setting("CLOUDFLARE_D1_DATABASE_ID")
    if account and database_id:
        return D1Target(account, database_id, "settings")

    from control_plane.models import ProviderInventory

    name = _setting("CLOUDFLARE_D1_DATABASE_NAME")
    snapshot = ProviderInventory.objects.filter(kind=D1_KIND).first()
    candidates = [
        record
        for record in (snapshot.records if snapshot else [])
        if isinstance(record, dict) and record.get("uuid") and record.get("account_id")
    ]
    if account:
        candidates = [r for r in candidates if r["account_id"] == account]
    if database_id:
        candidates = [r for r in candidates if r["uuid"] == database_id]
    elif name:
        candidates = [r for r in candidates if r.get("name") == name]

    if len(candidates) == 1:
        found = candidates[0]
        return D1Target(
            account or str(found["account_id"]),
            database_id or str(found["uuid"]),
            f"{D1_KIND} reading",
        )
    if not candidates:
        qualifier = (
            f" id {database_id!r}"
            if database_id
            else f" name {name!r}"
            if name
            else ""
        )
        raise D1Error(
            f"Cloudflare D1 is not configured: no stored {D1_KIND} reading "
            f"matches{qualifier}. Set CLOUDFLARE_ACCOUNT_ID and "
            "CLOUDFLARE_D1_DATABASE_ID, or give the cloudflare_api connection "
            "D1 Read."
        )
    names = ", ".join(sorted(str(r.get("name", "")) for r in candidates))
    raise D1Error(
        f"Cloudflare D1 is ambiguous: {counted(len(candidates), 'database')} "
        f"match ({names}). Set CLOUDFLARE_D1_DATABASE_NAME or "
        "CLOUDFLARE_D1_DATABASE_ID."
    )


def _endpoint() -> str:
    target = database()
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{target.account}"
        f"/d1/database/{target.database}/query"
    )


def query(sql: str, params: list | None = None) -> list[dict]:
    """
    Run one SQL statement against D1 and return the result rows as dicts.

    Works for SELECT (rows returned) and UPDATE/INSERT (empty list returned).
    """
    token = getattr(settings, "CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise D1Error("Cloudflare D1 is not configured. Set CLOUDFLARE_API_TOKEN.")

    body = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # An HTTPError is the error response itself, socket included, and
        # nothing closes it for us: chained below, it would stay open until
        # the D1Error was collected. Read what it says, then let it go.
        with exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
        raise D1Error(f"D1 API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise D1Error(f"Could not reach the D1 API: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise D1Error(f"Unexpected D1 API response: {exc}") from exc

    if not payload.get("success"):
        raise D1Error(f"D1 API error: {payload.get('errors')}")

    result = payload.get("result") or []
    if not result:
        return []
    return result[0].get("results", []) or []


def get_dashboard_state(limit: int = 4) -> tuple[list[dict], int]:
    """Recent submissions and the unread total from one D1 request."""

    # These rows are stored in HQ's database as a reading (contacts.inbox), so
    # a copy of each submitter's name lives there: the dashboard panel and
    # search show it without a D1 read. Email and message stay in D1 only.
    rows = query(
        "SELECT id, created_at, name, status, "
        "SUM(CASE WHEN status = 'unread' THEN 1 ELSE 0 END) OVER () AS unread_count "
        "FROM contact_submissions ORDER BY id DESC LIMIT ?",
        [limit],
    )
    unread = int(rows[0].get("unread_count", 0)) if rows else 0
    return (
        [
            {key: value for key, value in row.items() if key != "unread_count"}
            for row in rows
        ],
        unread,
    )


# The unread count and newest rows kept by contacts.inbox; a write here reads
# them again.
UNREAD = "contacts.unread"


def _changed() -> None:
    """Read the stored inbox state again after a write changed it."""

    from .inbox import refresh

    refresh(force=True)


def list_submissions(status: str = "", q: str = "", limit: int = 500) -> list[dict]:
    """Fetch submissions, optionally filtered by status and/or a search term."""
    cols = (
        "id, created_at, name, email, status, country, "
        "substr(message, 1, 160) AS message_preview"
    )
    where: list[str] = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("(name LIKE ? OR email LIKE ? OR message LIKE ?)")
        params.extend([f"%{q}%"] * 3)
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    return query(
        f"SELECT {cols} FROM contact_submissions {clause}"
        f"ORDER BY id DESC LIMIT ?",
        [*params, limit],
    )


def status_counts() -> dict[str, int]:
    """Return submission counts per status."""
    rows = query(
        "SELECT status, COUNT(*) AS n FROM contact_submissions GROUP BY status"
    )
    return {row["status"]: row["n"] for row in rows}


def set_status(pk: int, status: str) -> None:
    """Flip a submission's status without touching assignee or notes."""
    query(
        "UPDATE contact_submissions SET status = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        [status, pk],
    )
    _changed()


def delete_submission(pk: int) -> None:
    """Delete a submission permanently."""
    query("DELETE FROM contact_submissions WHERE id = ?", [pk])
    _changed()


def get_submission(pk: int) -> dict | None:
    """Fetch a single submission by ID."""
    rows = query("SELECT * FROM contact_submissions WHERE id = ?", [pk])
    return rows[0] if rows else None


def update_submission(pk: int, status: str, assigned_to: str, admin_notes: str) -> None:
    """Update an existing submission's metadata."""
    query(
        "UPDATE contact_submissions SET status = ?, assigned_to = ?, "
        "admin_notes = ?, updated_at = datetime('now') WHERE id = ?",
        [status, assigned_to, admin_notes, pk],
    )
    _changed()
