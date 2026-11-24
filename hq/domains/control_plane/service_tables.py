"""The services board's tables: every service under its group, and the names
HQ does not manage."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from django.utils.text import capfirst

from hq.platform.application.entity_links import EntityLink, entity_link, kind_label
from hq.platform.application.freshness import reading_age
from hq.platform.application.pages import PageAction, RowMark, RowOrder
from hq.platform.application.service_facets import CERTIFICATE_FACET, DNS_FACET, RUNTIME_FACET
from hq.platform.application.tables import Cell, DataTable, Group, Row, StatusMark, TableColumn
from hq.platform.core.templatetags.status_tags import drift_tip

from .cells import run


def services_table(
    groups: Sequence[tuple[str, Sequence[tuple[Any, RowMark | None, RowOrder | None]]]],
    facets: Sequence[tuple[str, str]],
    *,
    projects: bool,
    empty: str,
    start_url: str,
) -> DataTable:
    """Every service, a group each (its heading only where there are two),
    sharing one set of columns: its name, who it is open to, each facet the
    providers declare, where it runs, its project, its state, and its star and
    the pair that moves it. Columns leave a narrow table from the far end."""

    columns = (
        TableColumn("Service", kind="identifier"),
        TableColumn("Open to", drop=len(facets) + 3),
        *(TableColumn(label, drop=len(facets) - at + 2) for at, (_id, label) in enumerate(facets)),
        TableColumn("Runs on", drop=2),
        *((TableColumn("Project", drop=1),) if projects else ()),
        TableColumn("State", kind="status", css="key-col"),
    )
    shown = [facet_id for facet_id, _label in facets]
    return DataTable(
        "Services",
        columns,
        groups=tuple(
            Group(
                label if len(groups) > 1 else "",
                tuple(
                    Row(_service_cells(service, shown, projects=projects), mark=mark, order=order)
                    for service, mark, order in rows
                ),
            )
            for label, rows in groups
        ),
        region={"empty": empty, "action_url": start_url, "action_label": "Publish a service"},
    )


def _service_cells(service: Any, facets: list[str], *, projects: bool) -> tuple[Cell, ...]:
    exposure = service.exposure
    by_id = {facet.id: facet for facet in service.facets}
    health = [
        Cell(status=StatusMark(words, tone=tone, title=service.health.detail)) for tone, words in service.health.parts
    ]
    return (
        Cell.of(entity_link("service", service.hostname)),
        Cell(
            status=StatusMark(
                exposure.short, tone=exposure.tone, title=exposure.sentence, pill=False, data_exposure=exposure.level
            )
        ),
        *(_facet(service, by_id[facet_id]) if facet_id in by_id else Cell() for facet_id in facets),
        _runs_on(service),
        *((_project(service),) if projects else ()),
        run(health),
    )


def _facet(service: Any, facet: Any) -> Cell:
    """A facet's declarations, then the certificate the path presents (named
    by its issuer, by its own name only where no declaration names it) or a
    line per kind of reading."""

    lines = [replace(Cell.of(claim.within_service), readout=True) for claim in facet.claims]
    if facet.id == CERTIFICATE_FACET:
        certificate = service.path.certificate
        if certificate:
            note = (
                certificate.issuer
                or (certificate.name if not facet.claims else "")
                or ("not read" if certificate.unread else "")
            )
            lines.append(
                Cell(
                    certificate.role,
                    title=certificate.statement,
                    parts=(Cell(note, muted=True, dot=True),) if note else (),
                )
            )
    else:
        lines += [
            Cell(line.label, title=line.hint, parts=(Cell(line.detail, muted=True, dot=True),) if line.detail else ())
            for line in facet.reading_lines
        ]
        if not facet.claims and not facet.readings and service.is_observed and facet.id == DNS_FACET:
            lines += [Cell("No record", muted=True)] if service.path.gaps else []
    return Cell(lines=tuple(lines)) if lines else Cell()


def _runs_on(service: Any) -> Cell:
    """Where it runs, in one column: the origin's own phrasing, linked to the
    declaration behind it, where the runtime facet and the origin say one fact."""

    path = service.path
    if service.is_observed and path.observed:
        return Cell(lines=tuple(Cell.of(replace(hop.link, label=hop.source.short)) for hop in path.observed))
    if path.redirects_to:
        return Cell("Redirects to", parts=(Cell.of(entity_link("service", path.redirects_to)),))
    if path.ends_at is not None and path.ends_at.step == "origin":
        unread = (Cell("redirect not read", muted=True, dot=True),) if path.ends_at.unread else ()
        return Cell(path.ends_at.label, parts=unread)
    if service.origin:
        runtime = next((facet for facet in service.facets if facet.id == RUNTIME_FACET), None)
        url = runtime.claims[0].url if runtime and runtime.claims else ""
        qualifier = (Cell(service.origin.qualifier, muted=True),) if service.origin.qualifier else ()
        return Cell(service.origin.headline, url, cut=True, parts=qualifier)
    if service.served_by:
        return Cell(lines=tuple(Cell(name, cut=True) for name in service.served_by))
    return Cell.of(entity_link("machine", path.machine)) if path.machine else Cell()


def _project(service: Any) -> Cell:
    project = service.project
    return Cell.of(entity_link("project", project["slug"], label=project["name"])) if project else Cell()


def unmanaged_table(unmanaged: Sequence[tuple[Any, tuple[PageAction, ...]]]) -> DataTable:
    """Every name the providers hold that no declaration accounts for, with
    each facet's value and the way to take it under HQ's management where a
    connection that manages read it. One only observed says so."""

    facets = unmanaged[0][0].facets if unmanaged else ()
    columns = (
        TableColumn("Name", kind="identifier"),
        *(
            TableColumn(label, kind="identifier", drop=len(facets) - at + 1)
            for at, (_id, label, _value) in enumerate(facets)
        ),
    )
    return DataTable(
        "Unmanaged names",
        columns,
        tuple(
            Row(
                (
                    Cell(
                        service.hostname,
                        lines=() if actions else (Cell("Read only", muted=True),),
                    ),
                    *(Cell(value, readout=True) for _id, _label, value in service.facets),
                ),
                actions=actions,
            )
            for service, actions in unmanaged
        ),
    )


PART_COLUMNS = (
    TableColumn("Step"),
    TableColumn("Name", kind="identifier", drop=3),
    TableColumn("State", kind="status", css="key-col"),
    TableColumn("Impact", drop=2),
    TableColumn("Read from", drop=1),
)


def parts_table(parts: Sequence[Any], *, read_only: bool, here: str) -> DataTable:
    """One row per part of a service (``service_context.part_rows``): what it
    is, its health from its declaration or the reading that covers it, what
    changing it would do, and where the reading came from. A declaration no
    hop reaches closes the table. Its edit returns to ``here``."""

    return DataTable(
        "Steps",
        PART_COLUMNS,
        tuple(
            Row(
                (
                    Cell(part.label),
                    _part_name(part),
                    run(_part_states(part)),
                    Cell(part.consequence, muted=True),
                    _read_from(part),
                ),
                actions=_part_edit(part, read_only=read_only, here=here),
            )
            for part in parts
        ),
    )


def _part_edit(part: Any, *, read_only: bool, here: str) -> tuple[PageAction, ...]:
    """The edit its declaration offers, which comes back to ``here``."""

    claim = None if read_only else part.claim
    edit = (
        claim.offered.edit(claim.resource_key, **({"next": here} if here else {})) if claim and claim.offered else None
    )
    return (edit,) if edit else ()


def _part_name(part: Any) -> Cell:
    """The part by its link or name, the certificate it serves as a mark
    after it, and beneath it what the row's neighbors leave unsaid."""

    hop = part.hop
    if hop is None:
        readings = tuple(Cell(reading.value, muted=True) for reading in part.claim.readings[:1])
        return replace(Cell.of(part.claim.within_service), lines=readings)
    named = bool(hop.link and hop.link.url) or bool(hop.name)
    if hop.link and hop.link.url:
        value = Cell.of(hop.link)
    elif hop.name:
        value = Cell(hop.name)
    else:
        value = Cell(part.detail)
    certificate = hop.certificate
    marks = ()
    if certificate is not None and not certificate.unread:
        marks = (
            Cell(
                status=StatusMark(
                    certificate.statement,
                    tone="good" if certificate.attestation else "neutral",
                    pill=False,
                    icon="check-circle",
                    shown=False,
                    tip=certificate.tip,
                    data_tip_host=certificate.confirmed_on,
                )
            ),
        )
    lines = (
        *((Cell(part.detail, muted=True, shows="narrow-more"),) if part.detail and named else ()),
        *((Cell(certificate.line, muted=True),) if certificate is not None and certificate.unread else ()),
        *(Cell(relation, muted=True, parts=(Cell.of(link),)) for relation, link in hop.overlays),
        *((Cell(capfirst(hop.unread), muted=True),) if hop.unread else ()),
    )
    return replace(value, parts=marks, lines=lines)


def _part_states(part: Any) -> list[Cell]:
    """Its health, each reading changed outside HQ, and its certificate where unhealthy."""

    found = [Cell(status=StatusMark(part.health[0], tone=part.health[1]))] if part.health else []
    claim = part.claim
    if claim is not None and claim.drifted:
        found += [
            Cell(status=StatusMark("Changed outside HQ", tone="serious", tip=drift_tip(reading)))
            for reading in claim.readings
            if reading.drifted
        ]
    certificate = part.certificate_claim
    if certificate is not None and certificate.health["state"] != "healthy":
        found.append(
            Cell(
                status=StatusMark(
                    "Certificate",
                    certificate.health["label"].lower(),
                    tone=certificate.health["state"],
                    title=f"Certificate {certificate.resource_key}",
                )
            )
        )
    return found


def _read_from(part: Any) -> Cell:
    """Where the part's value was read: in HQ alone, or by a connection, and how long ago."""

    source = part.read_from
    if source is not None and source.declared:
        return Cell("In HQ, not read", muted=True)
    if source:
        age = (Cell(piece=reading_age(source.freshness), muted=True, dot=True),) if source.observed_at else ()
        if source.connection:
            through = Cell("through", muted=True, parts=(Cell.of(entity_link("connection", source.connection)),))
            return Cell(source.label, parts=age, lines=(through,))
        return Cell(source.label, parts=age)
    return Cell("In HQ, not on the path", muted=True) if part.hop is None else Cell()


ALIAS_COLUMNS = (
    TableColumn("Record", kind="identifier", cell=lambda each: Cell.of(EntityLink(**each["link"]))),
    TableColumn("Type", drop=2, cell=lambda each: Cell(kind_label(each["kind"]))),
    TableColumn("For", kind="identifier", drop=1, cell=lambda each: Cell(each["alias"])),
    TableColumn(
        "State",
        kind="status",
        css="key-col",
        cell=lambda each: Cell(each["health"]["label"], tone=each["health"]["state"]),
    ),
)


def aliases_table(aliases: Sequence[Any]) -> DataTable:
    """The declarations behind a service's other names, from its record."""

    return DataTable.of("Other names", ALIAS_COLUMNS, aliases)
