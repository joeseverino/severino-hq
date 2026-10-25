"""The months behind the estate: each record, and the audit event that says
it was made, dated to when it was really made and by whom.

The seed writes every record in one moment, so the audit log would read as a
single run by the system. Here each event takes its record's own date and its
real author: the operator for what they keep by hand and declare, the agent
for the runbooks it mirrors from the vault, and the system for scheduled work
and the controller's reads.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from hq.domains.calendars.models import Entry
from hq.domains.control_plane.models import ManagedResource
from hq.domains.jobs.models import Job
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt
from hq.platform.application import agent_registry, linked_accounts
from hq.platform.core.audit import audited_labels
from hq.platform.core.models import AgentIdentity, AuditLog, LinkedAccount
lazy from hq.domains.assets.models import Asset
lazy from hq.domains.content.models import ContentItem
lazy from hq.domains.docs_index.models import DocumentationRecord
lazy from hq.domains.expenses.models import Expense

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
SYNC = {"interface": "mcp", "actor": AGENT, "operation": "documentation.sync"}


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


# Each kind the estate writes: its model, who makes it, and when a record of
# it was made and last changed.
KINDS = (
    (Project, "operator", lambda p: (_days_back(PROJECTS[p.slug][0], p.slug), _days_back(PROJECTS[p.slug][1], p.slug))),
    (ManagedResource, "operator", lambda r: (_days_back(_resource_days(r.key), r.key),) * 2),
    (ContentItem, "operator", lambda c: (_on(c.published_at - timedelta(days=3), c.slug), _on(c.published_at, c.slug))),
    (Expense, "operator", lambda e: (_on(e.date, "expense", e.pk),) * 2),
    (Receipt, "operator", lambda r: (_on(r.date, "receipt", r.pk),) * 2),
    (Asset, "operator", lambda a: (_on(a.purchase_date, a.slug),) * 2),
    (Entry, "operator", lambda e: (_days_back(20 + int(hexed(e.title), 16) % 180, e.title),) * 2),
    (LinkedAccount, "operator", lambda a: (_days_back(MACHINE_DAYS, a.provider),) * 2),
    (
        DocumentationRecord,
        "agent",
        lambda d: (_days_back(MIRRORED[d.doc_id], d.doc_id), _on(d.last_reviewed, d.doc_id)),
    ),
    (AgentIdentity, "agent", lambda a: (_days_back(AGENT_DAYS, a.client_id),) * 2),
    (Job, "system", lambda j: (j.created_at,) * 2),
)


def settle(owner=None):
    """Date every record and its audit events, and name who made them.

    ``owner`` is the operator; without one, the database's first account is."""

    owner = owner or get_user_model().objects.order_by("pk").first()
    labels = {**audited_labels(), LinkedAccount: linked_accounts.AUDIT_LABEL, AgentIdentity: agent_registry.AUDIT_LABEL}
    for model, actor, made in KINDS:
        records = {str(record.pk): record for record in model.objects.all()}
        for event in AuditLog.objects.filter(
            object_type=labels[model], action__in=(AuditLog.Action.CREATED, AuditLog.Action.UPDATED)
        ):
            record = records.get(event.object_id)
            if record is None:
                continue
            created, changed = made(record)
            when = created if event.action == AuditLog.Action.CREATED else changed
            AuditLog.objects.filter(pk=event.pk).update(created_at=when, **_by(actor, event, owner))
        _date(model, records.values(), made)


def _by(actor, event, owner):
    if actor == "operator" and owner is not None:
        return {"user": owner}
    if actor == "agent":
        return {"metadata": {**(event.metadata or {}), **SYNC}}
    return {}


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
