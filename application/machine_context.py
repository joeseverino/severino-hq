"""Everything else HQ holds about a machine, gathered by what it already knows.

The machine page is where the estate is most densely related and said least
about it. A machine answers on a handful of names, and behind each name sits a
DNS record, a proxy host, a certificate and something running -- four
declarations HQ already resolves, per name, for the service page.

So this reads that answer rather than re-deriving it. An earlier version listed
every declaration that resolved to the machine and produced nineteen
undifferentiated rows: ten DNS records, eight proxy hosts and a certificate,
which is the same information the service model already organises by name, at
twice the volume and none of the structure. Emit once, derive everywhere means
the machine page asks the service catalog what supplies each name, and the
answer cannot disagree with the service page because it *is* the service page's
answer.

A section is a function from a machine to rows, and the registry below is the
list of them. A page that hand-wires each band can only grow when someone edits
it; a page that reads a registry grows when HQ learns something.

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
                if key.rpartition("-")[2].isdigit() and not key.rpartition("-")[2].startswith("0")
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


def _names(machine) -> ServiceSection | None:
    """Every name this machine answers on, and what supplies each one.

    The band the machine page exists for. A machine is only interesting through
    its names, and each name is supplied by a short, fixed set of things -- what
    runs it, what resolves it, what fronts it, what secures it. HQ already
    decides all four, per name, for the service page; this reads that decision
    rather than making a second one.

    Traffic rides along because it answers the question the facets raise. Four
    green cells beside a name nobody visits is a different situation from four
    green cells beside the name carrying the site, and only the machine page
    holds enough names at once for the comparison to mean anything.

    A blank cell is "nothing supplies this", which is not the same as unhealthy
    and not the same as unmeasured. Each is said in its own words.
    """

    from .services import service_catalog

    hostnames = tuple(getattr(machine, "hostnames", ()) or ())
    if not hostnames:
        return None

    # One derivation for the whole catalog rather than one per name: asking
    # per hostname would re-resolve the same estate nine times over.
    catalog = {service.hostname: service for service in service_catalog()}
    measured = traffic_for_hosts(set(hostnames), days=HOST_TRAFFIC_DAYS)
    facets = ("Runtime", "DNS", "Ingress", "Certificate")

    def supplied(service, label: str) -> Cell:
        facet = next((f for f in service.facets if f.label == label), None) if service else None
        claim = next(iter(facet.claims), None) if facet and facet.present else None
        if claim is None:
            return Cell("—", muted=True)
        return Cell(claim.resource_key, getattr(claim, "url", ""))

    def traffic(hostname: str) -> Cell:
        reading = measured.get(normalize_host(hostname))
        if not reading:
            return Cell("unmeasured", muted=True)
        return Cell(f"{reading['pageviews']:,}")

    ordered = sorted(
        hostnames,
        key=lambda host: (
            -(measured.get(normalize_host(host), {}).get("pageviews") or -1),
            host,
        ),
    )
    return ServiceSection(
        id="names",
        label=f"Names it answers · traffic over {HOST_TRAFFIC_DAYS} days",
        columns=("Name", *facets, "Pageviews"),
        records=tuple(
            (
                Cell(
                    hostname,
                    reverse("control_plane:service", kwargs={"hostname": hostname}),
                ),
                *(supplied(catalog.get(hostname), label) for label in facets),
                traffic(hostname),
            )
            for hostname in ordered
        ),
        # The whole graph, rather than more rows here. Every other relationship
        # this machine has is an edge, and the topology is where edges live.
        actions=(("See this machine in the topology", reverse("control_plane:topology")),),
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
    _names,
    _activity,
)
