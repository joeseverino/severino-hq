"""Every finding rule HQ knows, as the Checks page lists them: by the section
each raises its findings under, with its bars and how many it has open."""

from collections import Counter
from dataclasses import dataclass

from django.utils.text import capfirst, get_text_list

from .attention import INFRASTRUCTURE, rated
from .finding_model import FindingRule
from .findings import estate_findings, finding_rules
from .routes import reverse
from .security import Principal
from .tables import Cell, DataTable, Group, Row, TableColumn
from .tone import Tone
from .ui import ended
from .url_query import with_query
lazy from .domains import host_domains


@dataclass(frozen=True, slots=True)
class Check:
    """One rule, and how many findings it has open for the reader."""

    rule: FindingRule
    open: int


def _anchor(name: str) -> str:
    return f"check-{name}"


def check_url(name: str) -> str:
    """Where the check named ``name`` is read: its row on the Checks page."""

    return f"{reverse('control_plane:checks')}#{_anchor(name)}"


def _lines(*said: str) -> Cell:
    """Each of ``said`` on a line of its own."""

    first, *rest = said or ("",)
    return Cell(first, lines=tuple(Cell(line) for line in rest))


def _open(check: Check) -> Cell:
    if not check.open:
        return Cell(str(check.open), muted=True)
    return Cell(str(check.open), with_query(reverse("control_plane:findings"), {"rule": check.rule.name}))


COLUMNS = (
    TableColumn(
        "Check",
        cell=lambda check: Cell(
            check.rule.title,
            lines=(
                Cell(check.rule.name, readout=True, muted=True),
                *(
                    (Cell(f"Subsumes {get_text_list(list(check.rule.subsumes), 'and')}", shows="more"),)
                    if check.rule.subsumes
                    else ()
                ),
            ),
        ),
    ),
    TableColumn(
        "Severity", kind="status", cell=lambda check: Cell(check.rule.severity.urgency, tone=check.rule.severity)
    ),
    TableColumn(
        "Bars",
        drop=3,
        cell=lambda check: _lines(*(capfirst(rated(tone, bar)) for tone, bar in check.rule.bars.items())),
    ),
    TableColumn("Clears once", drop=2, cell=lambda check: Cell(capfirst(check.rule.clears))),
    TableColumn(
        "To resolve",
        drop=1,
        cell=lambda check: Cell(
            check.rule.operator_action, lines=(Cell(f"By hand: {check.rule.no_help_reason}", muted=True),)
        ),
    ),
    TableColumn("Open", kind="numeric", css="key-col", cell=_open),
)


def checks(principal: Principal) -> DataTable:
    """The table of every rule, grouped by the section it raises under, in
    the order the rules are declared."""

    raised = Counter(
        each.rule for finding in estate_findings(principal=principal) for each in (finding, *finding.consequences)
    )
    labels = {domain.id: domain.label for domain in host_domains()}
    grouped: dict[str, list[Row]] = {}
    for rule in finding_rules():
        check = Check(rule, raised[rule.name])
        grouped.setdefault(rule.domain or INFRASTRUCTURE, []).append(
            Row(
                tuple(column.cell(check) for column in COLUMNS if column.cell),
                id=_anchor(rule.name),
            )
        )
    return DataTable(
        "Checks",
        COLUMNS,
        groups=tuple(Group(labels.get(domain, domain), tuple(rows), counted=True) for domain, rows in grouped.items()),
    )


def tones() -> str:
    """The four severities, most severe first, and which reach Needs you."""

    every = get_text_list([tone.urgency for tone in Tone], "and")
    reach = get_text_list([tone.urgency for tone in Tone if tone.acted_on], "and")
    return f"Severities, most severe first: {ended(every)} {reach} reach Needs you."
