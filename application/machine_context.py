"""Everything else HQ holds about a machine, gathered by what it already knows.

The machine page is where the estate is most densely related and says least
about it. A machine is reached by connections, runs containers, answers on a
handful of names, holds declarations, and appears in the audit log under every
one of their keys -- and none of that needs a new column, because each side
already carries the thing that identifies the other.

So this is the service page's shape applied to a machine: a section is a
function from a machine to rows, and the registry below is the list of them.
A page that hand-wires each band can only grow when someone edits it; a page
that reads a registry grows when HQ learns something.

The section and cell primitives are shared with the service page rather than
copied, so a band gained on one renders identically on the other.
"""

from __future__ import annotations

from typing import Callable

from django.urls import reverse

from core.models import AuditLog

from .analytics import HOST_TRAFFIC_DAYS, normalize_host, traffic_for_hosts
from .service_context import Cell, ServiceSection


def sections_for(machine) -> tuple[ServiceSection, ...]:
    """Every section that has something to say about this machine."""

    found = []
    for resolve in SECTIONS:
        section = resolve(machine)
        if section is not None and (section.records or section.actions):
            found.append(section)
    return tuple(found)


def _traffic(machine) -> ServiceSection | None:
    """Which of this machine's names anyone actually visits.

    A machine answers on several names and they are not equally used -- an
    origin nobody reaches directly, an admin surface used twice a month, and the
    one name that carries the site. The machine page is the only place that
    question is even askable, because it is the only page that holds all the
    names at once.

    Unmeasured names are listed rather than dropped. "Nothing measures this" and
    "nobody visits this" are opposite conclusions and the row says which it is,
    because the first is a gap in HQ and the second is a fact about the estate.
    """

    hostnames = tuple(getattr(machine, "hostnames", ()) or ())
    if not hostnames:
        return None
    measured = traffic_for_hosts(set(hostnames), days=HOST_TRAFFIC_DAYS)
    if not measured:
        return None

    def row(hostname: str) -> tuple[Cell, ...]:
        reading = measured.get(normalize_host(hostname))
        if not reading:
            return (
                Cell(hostname),
                Cell("—", muted=True),
                Cell("—", muted=True),
                Cell("nothing measures this name", muted=True),
            )
        interval = reading.get("sample_interval") or 1
        return (
            Cell(hostname),
            Cell(f"{reading['pageviews']:,}"),
            Cell(f"{reading['visits']:,}"),
            Cell(
                "Counted" if interval <= 1 else f"Sampled 1 in {interval}",
                muted=interval > 1,
            ),
        )

    # Measured names first, busiest down: the question is which names carry the
    # traffic, and an alphabetical list buries the answer among the quiet ones.
    ordered = sorted(
        hostnames,
        key=lambda host: (
            -(measured.get(normalize_host(host), {}).get("pageviews") or -1),
            host,
        ),
    )
    return ServiceSection(
        id="traffic",
        label=f"Traffic by name · {HOST_TRAFFIC_DAYS} days",
        columns=("Name", "Pageviews", "Visits", "Basis"),
        records=tuple(row(hostname) for hostname in ordered),
        actions=(("Open analytics", reverse("analytics:overview")),),
    )


def _identity(machine) -> ServiceSection | None:
    """The declarations that constitute this machine, and what each is filed as.

    One machine is declared more than once -- as a machine, and again as the
    device the tailnet knows -- and a key is unique across every kind, so the
    second declaration is filed under a suffixed key. The estate then contains
    ``x`` and ``x-2`` describing one thing, which reads as two machines to
    anyone who meets the second one first.

    HQ has always known they were the same: the machine carries both keys. It
    simply never said so. This says so, because a name that looks like a
    duplicate is worth one row to explain and expensive to rediscover.
    """

    from control_plane.models import ManagedResource

    keys = [
        key
        for key in (
            getattr(machine, "declaration", ""),
            getattr(machine, "route_approval_key", ""),
        )
        if key
    ]
    if not keys:
        return None
    by_key = {r.key: r for r in ManagedResource.objects.filter(key__in=keys)}
    records = tuple(
        (
            Cell(key, reverse("control_plane:detail", kwargs={"key": key})),
            Cell(by_key[key].kind if key in by_key else "—", muted=key not in by_key),
            Cell(
                "filed under a suffixed key — the plain one was taken"
                if key.rpartition("-")[2].isdigit()
                and not key.rpartition("-")[2].startswith("0")
                else "",
                muted=True,
            ),
        )
        for key in keys
    )
    return ServiceSection(
        id="identity",
        label="Declared as",
        columns=("Declaration", "Kind", "Note"),
        records=records,
    )


def _related(machine) -> ServiceSection | None:
    """Every declaration that is about this machine, and why it is.

    The machine page's central question, and the one nothing answered. A
    machine's own declarations were gathered by looking for a ``host`` field, so
    a container was found and everything that relates some other way was not.

    There are exactly two other ways, and HQ already writes both down. A
    declaration can point at where the machine *is* -- a DNS answer, a
    forwarding target, an endpoint -- which ``locate`` resolves to a machine
    name. Or it can be about a name the machine *answers on*, which a provider
    states through ``hostnames``. A provider joins this by declaring one of
    those, not by anything here learning what it is.

    One band rather than two. A record frequently relates both ways -- an
    internal DNS record answers this machine's address *and* names one of its
    hostnames -- and splitting them would describe two relationships where there
    is one, listing the same record twice. The reason travels in the row.

    Names are matched with ``certificate_covers`` rather than by set
    intersection, because the declared name is often a wildcard: the
    certificate covering this machine's ``hq`` host lists ``*.example.com`` and
    never lists the host at all. That function is the one place that decides
    what a wildcard answers for, and a second implementation of it here would be
    a second chance to get it subtly wrong.
    """

    from control_plane.models import ManagedResource
    from control_plane.providers import PROVIDERS, certificate_covers

    from .locate import machines_index

    name = getattr(machine, "name", "")
    if not name:
        return None
    index = machines_index()
    answers = {
        normalize_host(host) for host in (getattr(machine, "hostnames", ()) or ()) if host
    }

    # Its own declarations are what this machine *is*, not what relates to it —
    # and one of them is filed under a suffixed key, so a page that listed it
    # here would report a machine as pointing at itself under a name that looks
    # like a second machine. They belong to `_identity`.
    own = set(getattr(machine, "resources", ()) or ()) | {
        getattr(machine, "declaration", ""),
        getattr(machine, "route_approval_key", ""),
    } - {""}

    found: list[tuple[str, str, str]] = []
    for resource in ManagedResource.objects.filter(enabled=True).order_by("kind", "key"):
        if resource.key in own:
            continue
        spec = resource.spec or {}
        why = ""
        for field, value in spec.items():
            # A field holding one endpoint and a field holding several are the
            # same kind of claim, so both are read the same way.
            candidates = value if isinstance(value, (list, tuple)) else (value,)
            hit = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, str) and item.strip() and index.resolve(item) == name
                ),
                None,
            )
            if hit is not None:
                why = f"{field} → {hit}"
                break
        if not why:
            provider = PROVIDERS.get(resource.kind)
            declared = frozenset(provider.hostnames(spec)) if provider and provider.hostnames else frozenset()
            served = next(
                (host for host in sorted(answers) if certificate_covers(host, declared)), ""
            )
            if served:
                why = f"serves {served}"
        if why:
            found.append((resource.key, resource.kind, why))

    if not found:
        return None
    return ServiceSection(
        id="related",
        label="What relates to this machine",
        columns=("Declaration", "Kind", "Because"),
        records=tuple(
            (
                Cell(key, reverse("control_plane:detail", kwargs={"key": key})),
                Cell(kind),
                Cell(why, muted=True),
            )
            for key, kind, why in found
        ),
    )


def _activity(machine) -> ServiceSection | None:
    """What has recently happened to the things this machine holds.

    Audit entries name the object they changed, and the objects on a machine are
    its declarations -- so the tie is the key each already carries. Scoped to
    those keys rather than the whole log: this answers "what changed here", not
    "what changed".
    """

    keys = [str(key) for key in (getattr(machine, "resources", ()) or ())]
    if not keys:
        return None
    events = AuditLog.objects.filter(object_id__in=keys).order_by("-created_at")[:8]
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
        columns=("Object", "Change", "When"),
        records=records,
    )


def _ago(moment) -> str:
    from .ui import ago

    return ago(moment)


# The list of sections, stated once. A section with nothing to say returns
# nothing and does not appear, so the page grows a band only when HQ has one.
SECTIONS: tuple[Callable[[object], ServiceSection | None], ...] = (
    _identity,
    _traffic,
    _related,
    _activity,
)
