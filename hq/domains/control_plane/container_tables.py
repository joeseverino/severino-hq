"""The containers' tables: a machine's, every container, and each image
that needs acting on. A container's standing and its status are said by one
function each, so no table can say them its own way."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from django.utils.text import capfirst, slugify

from hq.platform.application.entity_links import declared_link, entity_link, web_url
from hq.platform.application.moments import ago
from hq.platform.application.tables import Below, Cell, DataTable, Row, StatusMark, TableColumn
from hq.platform.application.ui import counted, measured, severity_tone

from .cells import change

# The urgent findings an image's open line names; its page lists them all.
_URGENT_SHOWN = 3


def standing_cells(standing: Any) -> tuple[Cell, Cell]:
    """What is known against the version that runs, and whether a newer one
    is published, each a status led by its mark. A newer version is its name
    alone, heard as available."""

    if standing.state == "vulnerable":
        known = Cell(
            status=StatusMark(
                standing.vulnerabilities,
                tone=standing.vulnerability_tone,
                pill=False,
                icon="shield",
                tip=standing.vulnerabilities_note,
            )
        )
    else:
        known = Cell(capfirst(standing.vulnerabilities), muted=True, tip=standing.vulnerabilities_note)
    if standing.update_state == "available":
        newer = standing.latest or standing.update
        update = Cell(
            status=StatusMark(newer, tone="attention", pill=False, icon="deploy"),
            aloud=f"{newer} available" if standing.latest else "",
        )
    elif standing.update_state == "current":
        update = Cell(status=StatusMark(standing.update, tone="good", pill=False, icon="check-circle", ink="context"))
    else:
        update = Cell(capfirst(standing.update), muted=True, tip=standing.update_note)
    return known, update


def status_cell(container: Any, up: str) -> Cell:
    """Its state where it is not running, then how long it has been up and its
    health check."""

    check = container.check
    up = up or container.status
    said = Cell(f"{up}, {check}" if check else up, role="data", title=capfirst(check.said) if check else "")
    return replace(Cell(status=StatusMark(container.state)), lines=(said,)) if not container.state.running else said


MACHINE_COLUMNS = (
    TableColumn("Container", kind="identifier"),
    TableColumn("Image", kind="identifier", drop=4),
    TableColumn("Vulnerabilities", kind="status", drop=3),
    TableColumn("Update", kind="status", drop=2),
    TableColumn("Ports", kind="identifier", css="optional-col", drop=1),
    TableColumn("Status", kind="status", css="key-col"),
)


def machine_containers(
    label: str, found: Sequence[Any], standings: Mapping[str, Any], uptimes: Mapping[str, str]
) -> DataTable:
    """One machine's containers, those on show or those folded away, each
    addressed by name so a moment in the history links to its row. A
    container HQ watches leads to its own page; its compose project shows
    only when it is not its own name."""

    def cells(container: Any) -> tuple[Cell, ...]:
        standing = standings.get(container.name)
        name = (
            Cell.of(entity_link("resource", container.watcher, label=container.name), name=True)
            if container.watcher
            else Cell(container.name, name=True)
        )
        stack = container.stack if container.stack and container.stack != container.name else ""
        image = (
            Cell(standing.label, readout=True, tip=container.image)
            if standing
            else Cell(container.image_label, readout=True)
        )
        return (
            replace(name, lines=(Cell(stack, muted=True),) if stack else ()),
            image,
            *(standing_cells(standing) if standing else (Cell(), Cell())),
            Cell(container.published),
            status_cell(container, uptimes.get(container.name) or container.uptime),
        )

    return DataTable(
        label,
        MACHINE_COLUMNS,
        tuple(Row(cells(container), id=f"container-{slugify(container.name)}") for container in found),
    )


EVERY_COLUMNS = (
    TableColumn("Container", kind="identifier"),
    TableColumn("Image", kind="identifier", drop=5),
    TableColumn("Vulnerabilities", kind="status", drop=4),
    TableColumn("Update", kind="status", drop=3),
    TableColumn("Hardening", kind="status", css="optional-col", drop=2),
    TableColumn("Serves", kind="identifier", css="optional-col", drop=1),
    TableColumn("Status", kind="status", css="key-col"),
)


def every_container(found: Sequence[Any]) -> DataTable:
    """Every container, one row each: where it runs, what it runs and how it
    is pinned, its standing, how it is hardened, what it serves and its status."""

    return DataTable(
        "All containers",
        EVERY_COLUMNS,
        tuple(
            Row(
                (
                    Cell(
                        item.running.name,
                        item.url,
                        name=True,
                        lines=(Cell("on", muted=True, parts=(Cell.of(item.machine.link),)),),
                    ),
                    _image(item.standing),
                    *standing_cells(item.standing),
                    _hardening(item.posture),
                    Cell(lines=tuple(Cell.of(entity_link("service", hostname)) for hostname in item.serves)),
                    status_cell(item.running, item.uptime),
                )
            )
            for item in found
        ),
    )


def _image(standing: Any) -> Cell:
    """The image by its short name, then its tag, how it is pinned and when it was built."""

    pinned = (
        Cell("locked", tip=f"Locked to {standing.image.digest[:19]}…", dot=True)
        if standing.pinned
        else Cell("tag only", tip="It runs whatever the tag pointed at when it was pulled.", dot=True)
    )
    built = (Cell(f"built {ago(standing.built)}", dot=True),) if standing.built else ()
    return Cell(
        standing.image.short,
        readout=True,
        lines=(Cell(standing.tag or "no version tag", muted=True, parts=(pinned, *built)),),
    )


def _hardening(posture: Any) -> Cell:
    """What its hardening checks found: the one that lets it control its
    machine, else how many are left to fix, else how many pass."""

    if posture.serious:
        return Cell(status=StatusMark("Can control its machine", tone="serious", pill=False, tip=posture.serious_unmet))
    if posture.unmet:
        return Cell(f"{len(posture.unmet)} to fix", tip="\n".join(result.check.unmet for result in posture.unmet))
    if posture.measured:
        passing = measured(posture.measured, "pass")
        return Cell(f"All {passing}" if posture.met == posture.measured else f"{posture.met} of {passing}", muted=True)
    return Cell()


NEED_COLUMNS = (
    TableColumn("Image", kind="identifier"),
    TableColumn("Change", drop=2),
    TableColumn("Known", kind="status", drop=1),
    TableColumn("Runs as", css="key-col"),
)


def need_table(heading: str, groups: Sequence[Mapping[str, Any]]) -> DataTable:
    """Each image a reason holds, once however many containers run it: the
    change to make and its risk, what is known against it, and where it
    runs. Its open line says what the change takes and what it fixes."""

    return DataTable(heading, NEED_COLUMNS, tuple(Row(_need_cells(group)) for group in groups))


def _need_cells(group: Mapping[str, Any]) -> tuple[Cell, ...]:
    standing, plan = group["standing"], group["plan"]
    image = (
        change(standing.label, standing.latest, url=(standing.release or {}).get("url", ""), code=True)
        if standing.newer
        else Cell(standing.label, readout=True)
    )
    tone = "serious" if standing.serious else "attention"
    if plan and plan.fixes:
        known = Cell(
            status=StatusMark("Vulnerable", tone=tone),
            lines=(Cell(f"the change fixes {len(plan.fixes)} of {len(standing.known_vulnerabilities)}", muted=True),),
        )
    elif standing.state == "vulnerable":
        known = Cell(status=StatusMark("Vulnerable", tone=tone))
    else:
        known = Cell()
    return (
        replace(image, below=(Below("", _need_lines(standing, plan)),)),
        Cell(plan.change_label, lines=(Cell(f"risk {plan.risk}", muted=True),))
        if plan
        else Cell("No newer release", muted=True),
        known,
        Cell(
            lines=tuple(
                Cell(item.running.name, item.url, muted=True, parts=(Cell(f"on {item.machine.name}"),))
                for item in group["containers"]
            )
        ),
    )


def _need_lines(standing: Any, plan: Any) -> tuple[Cell, ...]:
    """What the change takes, the tag that moved, and each advisory and urgent finding."""

    if plan:
        said = plan.risk_said
        said += f" Snapshot {counted(len(plan.data), 'mount,mounts')} first." if plan.stateful else ""
        said += f" Checked afterwards by {plan.verified_by[0]}." if plan.verified_by else ""
    else:
        said = f"{capfirst(standing.summary)}, and no newer release yet."
    if standing.release and standing.release.get("published"):
        said += f" Released {ago(standing.release['published'])}."
    moved = (
        (
            Cell(
                status=StatusMark(
                    "Its tag",
                    standing.tag,
                    "now names a newer build than the one running.",
                    tone="attention",
                    pill=False,
                )
            ),
        )
        if standing.moved_to
        else ()
    )
    advisories = tuple(
        Cell(
            status=StatusMark(advisory["severity"], tone=severity_tone(advisory["severity"])),
            role="prose" if advisory.get("summary") else "",
            parts=(
                Cell.of(declared_link(advisory["id"], web_url(advisory.get("url", "")), kind="dependency")),
                *((Cell(advisory["summary"]),) if advisory.get("summary") else ()),
            ),
        )
        for advisory in standing.advisories
    )
    urgent = tuple(
        Cell(
            status=StatusMark(finding["severity"], tone="serious"),
            parts=(
                Cell.of(declared_link(finding["id"], web_url(finding.get("url", "")), kind="dependency")),
                Cell(finding["package"], readout=True),
                change(finding["installed"], ", ".join(finding["fixed"])),
            ),
        )
        for finding in standing.urgent[:_URGENT_SHOWN]
    )
    return (Cell(said, role="prose"), *moved, *advisories, *urgent)
