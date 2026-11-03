"""The months behind the estate: each record, and the audit event that says
it was made, dated to when it was really made and by whom.

The seed writes every record in one moment, so without this the audit log
would read as a single run by the system. Each event's author is the operator
for what they keep by hand and declare, in the browser, the agent for the
runbooks it mirrors from the vault, over MCP (``doors``). The scheduler's runs
record their own endings (``work.jobs``), as every job does.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from hq.domains.calendars.models import Entry
from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt
from hq.platform.application import agent_registry, linked_accounts
from hq.platform.application.documentation import SYNC as DOCUMENTATION_SYNC
from hq.platform.application.security import JOB_INTERFACE, MCP_INTERFACE
from hq.platform.core.audit import audited_labels, stamped
from hq.platform.core.models import AgentIdentity, AuditLog, LinkedAccount
lazy from hq.domains.assets.models import Asset
lazy from hq.domains.content.models import ContentItem
lazy from hq.domains.docs_index.models import DocumentationRecord
lazy from hq.domains.expenses.models import Expense

from . import doors
from .agents import AGENT
from .names import CONTAINERS, MACHINE, hexed
from .records import RUNBOOKS

# Each project: how many days ago it was started, and last edited.
PROJECTS = {
    "hq": (420, 1),
    "photo-library": (380, 3),
    "media-server": (340, 26),
    "self-hosted-git": (300, 12),
    "tailnet-hardening": (150, 8),
    "offsite-backups": (130, 2),
}
# The service each container serves, by the project that brought it.
BROUGHT = {"hq": "hq", "immich": "photo-library", "jellyfin": "media-server", "forgejo": "self-hosted-git"}
# When the machine was declared, before anything ran on it, and the agent first called.
MACHINE_DAYS = 425
AGENT_DAYS = 300
# The runbooks the agent mirrors: how many days ago each was first mirrored.
MIRRORED = {doc_id: written for doc_id, *_rest, written, _reviewed in RUNBOOKS}
SYNC = {"interface": MCP_INTERFACE, "actor": AGENT, "operation": DOCUMENTATION_SYNC}
# HQ's own scheduled work, which nobody asked for in the moment.
SCHEDULED = {"interface": JOB_INTERFACE, "actor": "system"}


def _on(day, *key) -> datetime:
    """A moment on ``day`` during waking hours, the same in every build, never ahead of now."""

    minutes = 8 * 60 + int(hexed("at", day, *key), 16) % (13 * 60)
    at = timezone.make_aware(datetime.combine(day, time(minutes // 60, minutes % 60)))
    return min(at, timezone.now() - timedelta(minutes=1))


def _days_back(days, *key) -> datetime:
    return _on(timezone.localdate() - timedelta(days=days), *key)


def _resource_days(key: str) -> int:
    """A declaration dates from the project that brought its service, or the machine's first months."""

    if key == MACHINE:
        return MACHINE_DAYS
    for container, *_rest in CONTAINERS:
        if key.startswith(f"{container}-"):
            project = BROUGHT.get(container)
            return PROJECTS[project][0] if project else MACHINE_DAYS - 10
    return MACHINE_DAYS - 15


def _by_hand(create: str, edit: str, key: str):
    """Kept by hand in the browser: made on the ``create`` page, changed on the
    ``edit`` page of the record, whose address names it by ``key``."""

    def door(record, action):
        if action == AuditLog.Action.CREATED:
            return doors.stamp(doors.page(create))
        return doors.stamp(doors.page(edit, **{key: getattr(record, key)}))

    return door


def _signed_in(_record, _action):
    """Made by the sign-in, from the accounts the identity provider names."""

    return doors.stamp(doors.page(doors.SIGN_IN))


def _mirrored(_record, _action):
    """Mirrored by the agent from the vault, one call over MCP each."""

    return {"metadata": SYNC, "operation_id": doors.stamp(doors.mcp())["operation_id"]}


# Each kind the estate writes: its model, the door its rows came through
# (``None``: the one the step that writes it binds), and when a record of it
# was made and last changed. A record kept by hand with no operator to keep it
# is HQ's own (``SCHEDULED``).
KINDS = (
    (
        Project,
        _by_hand("projects:create", "projects:edit", "slug"),
        lambda p: (_days_back(PROJECTS[p.slug][0], p.slug), _days_back(PROJECTS[p.slug][1], p.slug)),
    ),
    (
        ManagedResource,
        _by_hand("control_plane:create", "control_plane:edit", "key"),
        lambda r: (_days_back(_resource_days(r.key), r.key),) * 2,
    ),
    (
        ContentItem,
        _by_hand("content:create", "content:edit", "slug"),
        lambda c: (_on(c.published_at - timedelta(days=3), c.slug), _on(c.published_at, c.slug)),
    ),
    (Expense, _by_hand("expenses:create", "expenses:edit", "pk"), lambda e: (_on(e.date, "expense", e.pk),) * 2),
    (Receipt, _by_hand("receipts:create", "receipts:edit", "pk"), lambda r: (_on(r.date, "receipt", r.pk),) * 2),
    (Asset, _by_hand("assets:create", "assets:edit", "slug"), lambda a: (_on(a.purchase_date, a.slug),) * 2),
    (
        Entry,
        _by_hand("calendar:entry_new", "calendar:entry_edit", "uid"),
        lambda e: (_days_back(20 + int(hexed(e.title), 16) % 180, e.title),) * 2,
    ),
    (LinkedAccount, _signed_in, lambda a: (_days_back(MACHINE_DAYS, a.provider),) * 2),
    (
        DocumentationRecord,
        _mirrored,
        lambda d: (_days_back(MIRRORED[d.doc_id], d.doc_id), _on(d.last_reviewed, d.doc_id)),
    ),
    # Its first call names the door it was first seen through (``agents.seen``).
    (AgentIdentity, None, lambda a: (_days_back(AGENT_DAYS, a.client_id),) * 2),
)


@contextmanager
def dated(owner=None) -> Iterator[None]:
    """Each audit event the block records about a record of ``KINDS``, written
    dated to when the record was made or last changed, and by whom.

    ``owner`` is the operator; without one, the database's first account is.
    """

    owner = owner or get_user_model().objects.order_by("pk").first()
    labels = {**audited_labels(), LinkedAccount: linked_accounts.AUDIT_LABEL, AgentIdentity: agent_registry.AUDIT_LABEL}
    kinds = {model: (labels[model], door, made) for model, door, made in KINDS}

    def stamp(record, action, object_type):
        label, door, made = kinds.get(type(record), (None, None, None))
        if object_type != label or action not in (AuditLog.Action.CREATED, AuditLog.Action.UPDATED):
            return None
        created, changed = made(record)
        return {
            "created_at": created if action == AuditLog.Action.CREATED else changed,
            **_by(door, owner, record, action),
        }

    with stamped(stamp):
        yield


def settle():
    """Date every record the way its audit events are (``dated``)."""

    for model, _door, made in KINDS:
        _date(model, model.objects.all(), made)


def _by(door, owner, record, action):
    """Who wrote the row and through which door: the agent its own, everyone
    else the operator, through theirs."""

    if door is None:
        return {}
    if door is _mirrored:
        return door(record, action)
    if owner is None:
        return {"metadata": SCHEDULED}
    return {"user": owner, **door(record, action)}


def _date(model, records, made):
    """The record's own timestamps, where it keeps them."""

    fields = {field.name for field in model._meta.concrete_fields}
    for record in records:
        created, changed = made(record)
        stamped = {
            name: value
            for name, value in (("created_at", created), ("updated_at", changed), ("first_seen", created))
            if name in fields
        }
        if stamped:
            model.objects.filter(pk=record.pk).update(**stamped)
