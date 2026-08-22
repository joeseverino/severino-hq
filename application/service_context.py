"""Everything else HQ holds about a service, gathered by the name it is.

HQ is two halves. One is densely related -- a project links to its content, its
assets, its expenses and its documents, each of those back again. The other is
infrastructure, where a connection relates to nothing, an inventory relates to
nothing, and a declaration relates only to its own operations. Nothing joins the
halves, so a page about a running service could say what reconciled it and
nothing about what it *is*.

The join is the name. A project publishes at a hostname, a document names the
system it describes, an audit entry names the resource it changed. None of that needs a foreign key: every side already carries
the thing that identifies the other, and storing the tie again would make two
answers where there is one.

So a section is a function from a service to rows, and the registry below is the
list of them. Adding what HQ knows next -- workflow runs, deployments, an
uptime history -- is one function and one entry, and the page renders it without
learning anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from django.urls import reverse

from core.models import AuditLog
from .services import projects_by_hostname


@dataclass(frozen=True)
class Cell:
    """One value in a section's table, and where it goes if anywhere."""

    text: str
    url: str = ""
    # Leaves HQ. Rendered so the operator knows before clicking, and so the
    # linked page cannot reach back through window.opener.
    external: bool = False
    muted: bool = False


@dataclass(frozen=True)
class ServiceSection:
    """One band under a service: a heading, columns, and rows of cells.

    A table rather than a list because that is what the rest of HQ shows and
    what everything else this will hold turns out to be -- workflow runs, deploys
    and changes are all a few named columns and a row each.
    """

    id: str
    label: str
    columns: tuple[str, ...]
    records: tuple[tuple[Cell, ...], ...]
    # ``(label, url)`` for the one thing worth doing about this section. Held as
    # data so a section that gains an action -- redeploy, open the run -- needs
    # no template change.
    actions: tuple[tuple[str, str], ...] = ()


def sections_for(service) -> tuple[ServiceSection, ...]:
    """Every section that has something to say about this service."""

    project = projects_by_hostname().get(service.hostname)
    found = []
    for resolve in SECTIONS:
        section = resolve(service, project)
        if section is not None and (section.records or section.actions):
            found.append(section)
    return tuple(found)


def _delivery(service, project) -> ServiceSection | None:
    """Where the code for this comes from, and when it last moved.

    The first place a power-user action belongs: this is the section that knows
    the repository, so redeploying or opening a run is an entry in ``actions``
    or another column here rather than a new panel.
    """

    if project is None:
        return None
    return ServiceSection(
        id="delivery",
        label="Delivery",
        columns=("Project", "Repository", "Last push"),
        records=(
            (
                Cell(
                    project.name,
                    reverse("projects:detail", kwargs={"slug": project.slug}),
                ),
                Cell(
                    _repository_label(project.repository_url),
                    project.repository_url,
                    external=True,
                )
                if project.repository_url
                else Cell("—", muted=True),
                Cell(
                    _ago(project.last_push_at) if project.last_push_at else "—",
                    muted=not project.last_push_at,
                ),
            ),
        ),
    )


def _repository_label(url: str) -> str:
    """``owner/name`` rather than the whole URL, which is mostly scheme."""

    path = urlparse(url).path.strip("/")
    return path or url


def _ago(moment) -> str:
    from django.utils.timesince import timesince

    return f"{timesince(moment)} ago"


def _activity(service, project) -> ServiceSection | None:
    """What has recently happened to the things behind this name.

    Audit entries name the object they changed, and the objects behind a service
    are its resources -- so the tie is the key each already carries. Kept to the
    resources rather than the whole log: this answers "what changed here", not
    "what changed".
    """

    keys = [claim.resource_key for claim in service.claims]
    if not keys:
        return None
    events = AuditLog.objects.filter(object_id__in=keys).order_by("-created_at")[:6]
    records = tuple(
        (
            Cell(
                event.object_repr or event.object_id,
                reverse("core:audit_detail", kwargs={"pk": event.pk}),
            ),
            Cell(event.get_action_display()),
            Cell(_ago(event.created_at), muted=True),
        )
        for event in events
    )
    if not records:
        return None
    return ServiceSection(
        id="activity",
        label="Recent changes",
        columns=("Resource", "Change", "When"),
        records=records,
    )


# The list of sections, stated once. A section that has nothing to say returns
# nothing and does not appear, so the page grows a band only when HQ has one.
SECTIONS: tuple[Callable[[object, object], ServiceSection | None], ...] = (
    _delivery,
    _activity,
)
