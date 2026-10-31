"""Everything else HQ holds about a service, gathered by the name it is.

No key joins HQ's densely related records (projects, content, assets,
documents) to its infrastructure, so without this a page about a running
service says what reconciled it and nothing about what it *is*. The join is the
name a project publishes at, a document describes, an audit entry changed:
every side already carries it, and storing the tie again would make two answers.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from hq.domains.control_plane.names import normalized_hostname
from hq.platform.application.routes import reverse
from hq.platform.core.models import AuditLog
lazy from hq.domains.projects.models import Project
lazy from hq.platform.core.network import split_host_port

from .analytics import HOST_TRAFFIC_DAYS, traffic_for_hosts
from .display_words import display
from .entity_links import EntityLink, entity_link, kind_label
from .moments import ago
from .published_sites import projects_by_hostname
from .ui import MISSING, PAGE_SECTION_ID, counted, ended
lazy from . import machines
lazy from .containers import standings_on
lazy from .github_estate import repository_for
lazy from .infrastructure import RESOURCE_TONES
lazy from .npm_sections import access
lazy from .pages import PageBadge
lazy from .path_dependencies import consequence_of
lazy from .path_model import Route
lazy from .paths import hq_path


@display
class Cell:
    """One value in a section's table, and where it goes if anywhere."""

    text: str
    url: str = ""
    # Leaves HQ. Rendered so the operator knows before clicking, and so the
    # linked page cannot reach back through window.opener.
    external: bool = False
    muted: bool = False
    # An entity mention, rendered through the link builder's answer.
    link: EntityLink | None = None

    @classmethod
    def of(cls, link: EntityLink, *, muted: bool = False) -> Cell:
        """A cell naming one entity, from ``entity_link``."""

        return cls(link.label, link.url, external=link.external, muted=muted, link=link)


@dataclass(frozen=True, slots=True)
class ServiceSection:
    """One band under a service: a heading, columns, and rows of cells.

    A table rather than a list because that is what the rest of HQ shows and
    what everything else this will hold turns out to be: workflow runs, deploys
    and changes are all a few named columns and a row each.
    """

    id: str
    label: str
    columns: tuple[str, ...]
    records: tuple[tuple[Cell, ...], ...]
    # ``(label, url)`` for the one thing worth doing about this section. Held as
    # data so a section that gains an action (redeploy, open the run) needs
    # no template change.
    actions: tuple[tuple[str, str], ...] = ()
    # ``(label, text)`` shown read-only under a disclosure, for raw readouts.
    readouts: tuple[tuple[str, str], ...] = ()
    # Rendered as a compact table.
    compact: bool = False
    # The reading kinds whose records it lists, so a page leaves those out of
    # its Relationships rather than saying them twice.
    renders: tuple[str, ...] = ()
    # Reference rather than a glance: shown folded, its size in the heading.
    folded: bool = False

    def __post_init__(self) -> None:
        if not PAGE_SECTION_ID.fullmatch(self.id):
            raise ValueError("ServiceSection id must be a valid page section id.")
        if not self.label.strip():
            raise ValueError("ServiceSection label must not be empty.")
        # A column no row has anything in is a heading over nothing. Dropped
        # here, so no section has to check its own; an unknown value (the dash placeholder) is
        # something to say and keeps its column.
        keep = filled_columns(len(self.columns), self.records)
        if self.records and len(keep) < len(self.columns):
            object.__setattr__(self, "columns", tuple(self.columns[i] for i in keep))
            object.__setattr__(self, "records", tuple(tuple(row[i] for i in keep) for row in self.records))


def filled_columns(width: int, records: tuple[tuple[Cell, ...], ...]) -> tuple[int, ...]:
    """The positions of the columns at least one row says something in."""

    return tuple(index for index in range(width) if any(index < len(row) and _says(row[index]) for row in records))


def _says(cell: Cell) -> bool:
    """Whether a cell shows anything: text, a link, or an entity."""

    return bool(cell.text.strip() or cell.url or cell.link)


def sections_for(service) -> tuple[ServiceSection, ...]:
    """Every section that has something to say about this service."""

    project = projects_by_hostname().get(service.hostname)
    found = []
    for resolve in SECTIONS:
        section = resolve(service, project)
        if section is not None and (section.records or section.actions):
            found.append(section)
    return tuple(found)


def _repository_label(url: str) -> str:
    """``owner/name`` rather than the whole URL, which is mostly scheme."""

    path = urlparse(url).path.strip("/")
    return path or url


def _activity(service, _project) -> ServiceSection | None:
    """What has recently happened to the things behind this name.

    Audit entries name the object they changed, and the objects behind a service
    are its resources, so the tie is the key each already carries. Kept to the
    resources rather than the whole log: this answers "what changed here", not
    "what changed".
    """

    return recent_changes([claim.resource_key for claim in service.claims], what="Resource", limit=6)


def recent_changes(keys: list[str], *, what: str, limit: int) -> ServiceSection | None:
    """The latest audit entries on the records ``keys`` name, as a section; None
    when there are none. The machine page asks the same of its declarations."""

    if not keys:
        return None
    events = AuditLog.objects.filter(object_id__in=keys).order_by("-created_at")[:limit]
    records = tuple(
        (
            Cell(
                event.object_repr or event.object_id,
                reverse("core:audit_detail", kwargs={"pk": event.pk}),
            ),
            Cell(event.get_action_display()),
            Cell(ago(event.created_at), muted=True),
        )
        for event in events
    )
    if not records:
        return None
    return ServiceSection(
        id="activity",
        label="Recent changes",
        columns=(what, "Change", "When"),
        records=records,
    )


def _traffic(service) -> SummaryItem | None:
    """What this host actually served, for the hosts something measures.

    Joined by the hostname analytics stores a reading against. A service
    nothing measures has no traffic fact at all: "0 pageviews" would read as a
    dead site rather than an unmeasured one, which are opposite conclusions.
    """

    hostname = service.hostname
    if not hostname:
        return None
    measured = traffic_for_hosts({hostname}, days=HOST_TRAFFIC_DAYS).get(normalized_hostname(hostname))
    if not measured:
        return None
    interval = measured.get("sample_interval") or 1
    # Carried rather than hidden: a figure extrapolated from one beacon in ten
    # is the best number available and still not a count.
    basis = "counted" if interval <= 1 else f"sampled 1 in {interval}"
    return SummaryItem(
        f"Traffic · {HOST_TRAFFIC_DAYS} days",
        counted(measured["pageviews"], "pageview"),
        detail=f"{counted(measured['visits'], 'visit')} · {basis}",
        icon="eye",
    )


# ----- Summary ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SummaryItem:
    """One line of a service's summary: a label, a value, and what backs it."""

    label: str
    value: str
    link: EntityLink | None = None
    detail: str = ""
    tone: str = ""
    # What kind of fact it is, drawn as its tile: a name in partials/_icon.html.
    icon: str = ""


def service_summary(service) -> tuple[SummaryItem, ...]:
    """What it is, where it runs, and whether it is healthy.

    Not the path: the path section follows at once and draws it hop by hop,
    so a one-line copy here would say it twice.
    """

    what = _what(service)
    return tuple(
        item
        for item in (
            SummaryItem(
                "Health",
                service.health.label,
                detail=_health_detail(service),
                tone=service.health.state,
                icon="activity",
            ),
            # Where it runs only when there is no path to show it: the path
            # names the machine and the container hop by hop.
            None if service.path.routes else replace(_where(service.path), icon="server"),
            # Only when it says something: "declared" is true of nearly every
            # name on the board, and first on the page it tells nobody anything.
            SummaryItem("What it is", what, icon="layers") if what else None,
            _certificate(service.path),
            _project(service),
            _traffic(service),
        )
        if item is not None
    )


def _health_detail(service) -> str:
    """What the health rests on, and what the container itself says: its
    uptime and its own health check."""

    # The uptime is the container's card on the path; the health check is the
    # evidence the verdict rests on.
    running = service.container
    # Not twice: a health check that passes is already the verdict's word.
    check = running.check if running and running.check.lower() != service.health.label.lower() else ""
    return " · ".join(part for part in (check, service.health.detail) if part)


def _what(service) -> str:
    if service.is_hq:
        return "HQ's own service"
    if service.is_observed:
        seen = ", ".join(
            dict.fromkeys(
                f"{hop.source.label} {hop.name}".strip() if hop.source else hop.name for hop in service.path.observed
            )
        )
        return f"Read through {ended(seen)} Not in HQ's settings." if seen else "Read"
    # Declared or not is what Health already says ("Nothing declared"), so
    # repeating it here would be the same fact twice.
    return ""


def _where(path) -> SummaryItem:
    """Where a request for the name ends: another name, a reading, a machine, or unknown."""

    end = path.ends_at
    if path.redirects_to:
        return SummaryItem(
            "Where it runs",
            f"Redirects to {path.redirects_to}",
            entity_link("service", path.redirects_to),
        )
    if end is not None and end.step == "hq" and path.machine:
        # HQ's own name ends at HQ; where it runs is the machine HQ is on.
        return SummaryItem("Where it runs", path.machine, entity_link("machine", path.machine), end.detail)
    if end is not None and end.step in ("served", "external", "origin", "hq"):
        return SummaryItem("Where it runs", f"{end.label} {end.name}".strip(), end.link, end.detail)
    if path.machine:
        container = next(
            (hop.name for route in path.routes for hop in route.hops if hop.step == "container"),
            "",
        )
        return SummaryItem(
            "Where it runs",
            path.machine,
            entity_link("machine", path.machine),
            _container_detail(path.machine, container),
        )
    return SummaryItem("Where it runs", MISSING, detail=path.gaps[0] if path.gaps else "")


def _container_detail(machine: str, container: str) -> str:
    """The container, and whether what it runs is current and safe."""

    if not container:
        return ""

    standing = standings_on(machine).get(container)
    return f"Container {container} · {standing.summary}" if standing else f"Container {container}"


def _certificate(path) -> SummaryItem | None:
    """The certificate a client is served, when a hop reads one."""

    certificate = next(
        (
            hop.certificate
            for route in path.routes[:1]
            for hop in route.hops
            if hop.certificate is not None and not hop.certificate.unread and hop.certificate.name
        ),
        None,
    )
    if certificate is None:
        return None
    return SummaryItem(
        "Certificate",
        certificate.name,
        certificate.link,
        " · ".join(part for part in (certificate.issuer, certificate.left) if part),
        icon="lock",
    )


def _project(service) -> SummaryItem | None:
    """The project that publishes it: where its code is, when it last moved,
    and what the GitHub App says is open or not yet deployed."""

    link = service.project_link
    if link is None:
        return None

    project = Project.objects.filter(slug=service.project["slug"]).only("repository_url", "last_push_at").first()
    repo = repository_for(project.repository_url) if project and project.repository_url else None
    parts = [
        _repository_detail(repo)
        or (_repository_label(project.repository_url) if project and project.repository_url else ""),
        f"pushed {ago(project.last_push_at)}" if project and project.last_push_at else "",
    ]
    return SummaryItem(
        "Project", service.project["name"], link, " · ".join(part for part in parts if part), icon="commit"
    )


def _repository_detail(repo) -> str:
    if repo is None:
        return ""

    head, deployed = (repo.head or {}).get("sha", ""), (repo.production or {}).get("sha", "")
    parts = [repo.short]
    if repo.pull_requests:
        parts.append(counted(len(repo.pull_requests), "open pull request", "open pull requests"))
    if head and deployed and head != deployed:
        parts.append(f"{repo.default_branch} is ahead of production")
    return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class PartRow:
    """One part of a service, with everything the page knows about it.

    A hop of the path, joined by its resource to the declaration that supplies
    it (its health, and where to edit it) and to what changing it would do. A
    declaration no hop reaches is a row too, with no hop, so nothing the
    service is made of goes unshown.
    """

    hop: Any = None
    claim: Any = None
    consequence: str = ""
    # The declaration behind the certificate this hop serves, when HQ holds one.
    certificate_claim: Any = None
    # ``(label, pill class)`` from whatever reading covers a hop no declaration
    # supplies: a machine's presence, the tailnet link, a container's state.
    observed_health: tuple[str, str] | None = None
    # Where the hop's value was read, when the hop itself carries none: the
    # tailnet link is the machine's tailnet device, a forward the proxy's.
    read_from: Any = None
    # The hop's detail, unless a neighboring row says it: a proxy's forward
    # target is the "Forwards to" row after it, a machine's address the
    # network row before it.
    detail: str = ""

    @property
    def label(self) -> str:
        return self.hop.label if self.hop is not None else kind_label(self.claim.kind)

    @property
    def health(self) -> tuple[str, str] | None:
        """``(label, pill class)``; read from an out-of-date reading, what it
        was when last read, in no tone of its own."""

        found: tuple[str, str] | None
        if self.claim is not None:
            tone = RESOURCE_TONES.get(self.claim.health["state"], "neutral")
            found = (self.claim.health["label"], _PILL_TONES[tone])
        else:
            found = self.observed_health
        if found is not None and self.read_from is not None and self.read_from.stale:
            return (f"{found[0]} when last read", _PILL_TONES["neutral"])
        return found


# A tone as the pill that draws it, so a declared part and a read one that
# are both fine look the same.
_PILL_TONES = {
    "good": "pill-reachable",
    "attention": "pill-attention",
    "serious": "pill-unreachable",
    "neutral": "pill-unprobed",
}


def part_rows(service, route) -> tuple[PartRow, ...]:
    """Each hop of ``route`` joined to its declaration, its health and its
    consequence, then the declarations no hop reached."""

    claims = {claim.url: claim for claim in service.claims}
    # Indexed in the whole walk, whose neighbors say what a row leaves out.
    shown = {id(hop) for hop in route.step_hops}
    rows = [_part_row(route.hops, index, claims) for index, hop in enumerate(route.hops) if id(hop) in shown]
    used = {item.url for row in rows for item in (row.claim, row.certificate_claim) if item is not None}
    rows.extend(PartRow(claim=claim) for url, claim in claims.items() if url not in used)
    return tuple(rows)


def _part_row(hops, index: int, claims: dict[str, Any]) -> PartRow:
    """The row of ``hops[index]``, joined to the declarations in ``claims``."""

    hop = hops[index]
    claim = claims.get(hop.link.url) if hop.link and hop.link.url else None
    certificate = hop.certificate
    certificate_claim = claims.get(certificate.link.url) if certificate is not None and certificate.link else None
    neighbors = {
        _shown(item)
        for item in (hops[index - 1] if index else None, hops[index + 1] if index + 1 < len(hops) else None)
        if item is not None
    }
    return PartRow(
        hop=hop,
        claim=claim,
        consequence=consequence_of(hop),
        certificate_claim=certificate_claim,
        observed_health=None if claim is not None else _observed_health(hops, index),
        read_from=hop.source or _read_from(hops, index),
        detail="" if hop.detail in neighbors else hop.detail,
    )


def _shown(hop) -> str:
    """What a hop's row shows as its value: its link's name, else its own."""

    if hop.link is not None and hop.link.url:
        return hop.link.label
    return hop.name or hop.detail


def _read_from(hops, index: int):
    """The reading behind a hop that is derived from its neighbor: a tailnet
    link from the machine's tailnet device, a forward from its proxy."""

    step = hops[index].step
    if step == "network":
        after = _next_step(hops, index, "machine")
        return after.source if after is not None else None
    if step == "upstream":
        before = _previous_step(hops, index, "ingress")
        return before.source if before is not None else None
    return None


def _next_step(hops, index: int, step: str):
    """The first hop after ``index`` at ``step``, or None."""

    return next((item for item in hops[index + 1 :] if item.step == step), None)


def _previous_step(hops, index: int, step: str):
    """The last hop before ``index`` at ``step``, or None."""

    return next((item for item in reversed(hops[:index]) if item.step == step), None)


def _observed_health(hops, index: int) -> tuple[str, str] | None:
    """A hop's health from the reading that covers it, when no declaration does."""

    reader = _HEALTH_READERS.get(hops[index].step)
    return reader(hops, index) if reader is not None else None


def _machine_health(hops, index: int) -> tuple[str, str] | None:
    """A machine's own state pill."""

    found = machines.machine(hops[index].name)
    return (found.state[0].capitalize(), f"pill-{found.state[1]}") if found else None


def _link_health(hops, index: int) -> tuple[str, str] | None:
    """A network hop's link, read from the tailnet presence of the machine the
    address belongs to: the next machine hop."""

    after = _next_step(hops, index, "machine")
    found = machines.machine(after.name) if after else None
    if found is None or found.presence is None:
        return None
    return ("Connected", "pill-reachable") if found.presence.online else ("Disconnected", "pill-unreachable")


def _container_health(hops, index: int) -> tuple[str, str] | None:
    """A container's state, or for a forward the state of the container behind
    it, as the machine before it last saw it running."""

    hop = hops[index]
    container = hop if hop.step == "container" else _next_step(hops, index, "container")
    on = _previous_step(hops, index, "machine")
    found = machines.machine(on.name) if on else None
    if container is None or found is None:
        return None
    running = next((item for item in found.containers if item.name == container.name), None)
    if running is None:
        return None
    return (running.state.capitalize(), "pill-reachable" if running.healthy else "pill-unreachable")


# The step each reading covers, and how its health is read.
_HEALTH_READERS: dict[str, Callable[[Any, int], tuple[str, str] | None]] = {
    "machine": _machine_health,
    "network": _link_health,
    "upstream": _container_health,
    "container": _container_health,
}


def routes_for(service, request) -> tuple:
    """The routes the page draws: the walked ones, except that on HQ's own name,
    viewed through that very name, the first is the route this request took,
    each hop joined to what the request showed (``paths.hq_path``).

    Only then: a request for another name travelled a different path and proves
    nothing about this one. The evidence itself is the connection page's.
    """

    routes = service.path.routes
    asked = normalized_hostname(split_host_port(request.get_host())[0])
    if asked != service.hostname or not routes:
        return routes
    walked = hq_path(request)
    if walked is None or walked.primary is None:
        return routes
    return (walked.primary, *routes[1:])


# The list of sections, stated once, each a function from a service to rows:
# what HQ knows next is one function and one entry, and the page renders it
# without learning anything. A section that has nothing to say returns nothing
# and does not appear, so the page grows a band only when HQ has one.
def _access(service, project) -> ServiceSection | None:
    return access(service, project)


SECTIONS: tuple[Callable[[object, object], ServiceSection | None], ...] = (
    _access,
    _activity,
)


def page_parts(service: Any, request: Any) -> dict[str, Any]:
    """Each route with its parts, each part joined to its declaration and to
    what changing it would do: one row per part. With no path to walk (its
    records unread), what is declared is still the service: listed as parts on
    no path rather than nowhere."""

    routes = routes_for(service, request)
    found: dict[str, Any] = {"routes": routes, "route_parts": [(route, part_rows(service, route)) for route in routes]}
    if not routes:
        found["declared_parts"] = part_rows(service, Route("", ()))
    return found


def missing_facets(service: Any) -> list[Any]:
    """The parts nothing declares: missing, each with how to add it, or found
    running and not declared, with what taking it on would mean. What is
    declared is a row of the parts table."""

    origin = service.origin
    return [
        facet
        for facet in service.facets
        if not facet.present
        and (facet.observed or not facet.readings)
        and not (origin is not None and origin.external and facet.routes)
    ]


def service_badges(service: Any, *, own: bool) -> tuple[Any, ...]:
    """Beside the hostname: who can reach it, and whether HQ may change it."""

    reach = service.reach
    found = []
    if reach.label:
        found.append(PageBadge(reach.label, "reachable" if reach.tailnet_only else "unprobed", reach.detail))
    if own:
        found.append(PageBadge("Read-only", title="HQ's own name: changed by deploying HQ"))
    return tuple(found)
