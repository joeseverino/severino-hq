"""The connections page's tables."""

from collections.abc import Iterable
from dataclasses import replace
from operator import attrgetter
from typing import Any

from hq.domains.control_plane.names import normalized_hostname
from hq.platform.application.entity_links import connection_anchor, declared_link
from hq.platform.application.freshness import reading_age
from hq.platform.application.moments import when
from hq.platform.application.pages import PageAction, offered_now
from hq.platform.application.routes import reverse
from hq.platform.application.tables import Below, Cell, DataTable, Group, Piece, Row, StatusMark, TableColumn
from hq.platform.application.ui import counted
from hq.platform.core.included_parts import CONNECTION_ABILITY, CONNECTION_READ, CREDENTIAL_FIX
from hq.platform.core.templatetags.action_tags import as_ask
from hq.platform.core.templatetags.value_tags import is_this_page


def _value(field: Any) -> Cell:
    """What goes in a field: its value, else the secret, else the reader's own."""

    if field.value:
        return Cell(field.value, readout=True)
    return Cell("the secret" if field.secret else "yours", muted=True)


FIELD_COLUMNS = (
    TableColumn("Field", kind="identifier", cell=lambda field: Cell(field.name, readout=True)),
    TableColumn("Value", kind="identifier", drop=2, cell=_value),
    TableColumn("Description", css="optional-col", drop=1, cell=lambda field: Cell(field.note, muted=True)),
)


def setup_fields(setups: Iterable[Any]) -> dict[str, DataTable]:
    """Each provider's 1Password item fields, by provider."""

    return {setup.provider: DataTable.of(f"{setup.label} fields", FIELD_COLUMNS, setup.fields) for setup in setups}


def connections_table(found: Any, request: Any) -> DataTable:
    """Every connection HQ has, a group for each part of HQ that uses some
    (``connections.purposes``), each part's heading only where there are
    two, so every part shares one set of columns."""

    purposes = tuple(found.purposes)
    return DataTable(
        "Connections",
        CONNECTION_COLUMNS,
        groups=tuple(
            Group(purpose if len(purposes) > 1 else "", tuple(_connection_rows(rows, request)), count=len(rows))
            for purpose, rows in purposes
        ),
    )


CONNECTION_COLUMNS = (
    TableColumn("Connection", kind="identifier"),
    TableColumn("Reads", drop=3),
    TableColumn("Reaches", css="optional-col", drop=1),
    TableColumn("State", kind="status", css="key-col"),
)


def _connection_rows(rows: Iterable[Any], request: Any) -> Iterable[Row]:
    """Each row, the line where its group starts said on its first row only:
    every row under it shares it, and repeating it says nothing new."""

    starts = None
    for row in rows:
        group, instance = row.group, row.connection.instance
        start = (group.spec.label, group.spec.secret_store, instance.keyless)
        yield _connection_row(row, request, starts=start != starts)
        starts = start


def _connection_row(row: Any, request: Any, *, starts: bool) -> Row:
    connection = row.connection
    # A way only to this page is its own state, and one its reader may not open is left out.
    actions = tuple(
        action
        for action in map(PageAction.of, (*filter(None, (connection.recommended_action,)), *connection.row_actions))
        if (action.method == "post" or not is_this_page({"request": request}, action.url)) and offered_now(action.url)
    )
    return Row(
        (
            replace(_name(row, starts=starts), below=_name_below(row)),
            Cell(
                parts=_pieces(row, CONNECTION_READ, "read", row.reads, attrgetter("label"), attrgetter("opens")),
                fit=True,
                below=(
                    (
                        Below(
                            "Changes",
                            _pieces(row, CONNECTION_ABILITY, "state", row.changes, attrgetter("ability.label")),
                        ),
                    )
                    if row.changes
                    else ()
                ),
            ),
            _reaches(connection.instance),
            _state(row),
        ),
        actions=actions,
        asks=(as_ask(row.read_now),) if row.read_now else (),
        id=connection_anchor(str(connection.instance.label or "")),
    )


def _name(row: Any, *, starts: bool) -> Cell:
    """The connection, its endpoint, where its group starts, how HQ reaches
    it, a refusal, what its last sweep saw, and what its credential lacks."""

    connection, instance, spec, sight = row.connection, row.connection.instance, row.group.spec, row.sight
    lines: list[Cell] = []
    if instance.endpoint:
        lines.append(Cell(instance.endpoint, readout=True, title=instance.endpoint))
    if starts:
        secrets = Cell("No key needed" if instance.keyless else f"Secrets in {spec.secret_store}", dot=True)
        links = (
            *((Cell("Docs", spec.documentation_url, dot=True),) if spec.documentation_url else ()),
            *((Cell("Manage", reverse(spec.management_route), dot=True),) if spec.management_route else ()),
        )
        first = (Cell(spec.label),) if spec.label != instance.label else ()
        head, *rest = (*first, replace(secrets, dot=bool(first)), *links)
        lines.append(replace(head, muted=True, role="prose", shows="narrow-more", parts=tuple(rest)))
    if reached := _reached(row):
        lines.append(reached)
    if sight is not None and sight.credential_refusal:
        lines.append(
            Cell(
                status=StatusMark(
                    sight.label, "refused this credential:", sight.credential_refusal, tone="serious", pill=False
                )
            )
        )
    if sight is not None and sight.sights:
        age = (Cell(piece=reading_age(at=sight.read_at), dot=True),) if sight.read_at else ()
        lines.append(Cell(sight.summary, muted=True, parts=age))
    if row.fix:
        lines.append(Cell(status=_fix_mark(row.fix)))
    return replace(Cell.of(connection.name_link), lines=tuple(lines))


def _reached(row: Any) -> Cell | None:
    """How HQ reaches it, then the facts its connection states, on one quiet line."""

    reach, facts = row.reach, row.connection.instance.facts
    said: list[Cell] = []
    if reach is not None and reach.summary:
        if reach.machine:
            said += [
                Cell(f"{reach.label} {reach.machine_word}"),
                replace(Cell.of(reach.machine), after="," if reach.relay else ""),
            ]
        else:
            said.append(Cell(reach.label, after="," if reach.relay else ""))
        said += [Cell(reach.relay)] if reach.relay else []
    # A controller that is the machine the reach names is said once.
    machine = normalized_hostname(reach.machine.label) if reach is not None and reach.machine else None
    facts = [fact for fact in facts if not (fact.label == "Controller" and normalized_hostname(fact.value) == machine)]
    said += [
        Cell(
            f"via {fact.value}" if fact.label == "Controller" else f"{fact.label}: {fact.value}",
            dot=bool(said) or at > 0,
        )
        for at, fact in enumerate(facts)
    ]
    if not said:
        return None
    head, *rest = said
    return replace(head, muted=True, shows="narrow-more", parts=tuple(rest))


def _fix_mark(fix: Any) -> StatusMark:
    """What a credential lacks or when it lapses, in a word or two."""

    if fix.missing:
        return StatusMark("Missing", counted(len(fix.missing), "permission"), tone="attention", pill=False)
    if fix.refused:
        return StatusMark("Credential refused", tone="attention", pill=False)
    return StatusMark("Expires", fix.expiry, tone="attention", pill=False)


def _name_below(row: Any) -> tuple[Below, ...]:
    """What else its credential would see, and the steps of its fix."""

    return (
        *((Below("Also sees", tuple(Cell(line) for line in row.more_scope)),) if row.more_scope else ()),
        *(
            (Below("To fix", (Cell(piece=Piece(CREDENTIAL_FIX, {"fix": row.fix}, ", ".join(row.fix.missing))),)),)
            if row.fix and (row.fix.missing or row.fix.steps)
            else ()
        ),
    )


def _pieces(
    row: Any, part: Any, key: str, items: Iterable[Any], label: Any, more: Any = lambda _item: False
) -> tuple[Cell, ...]:
    """Each of a connection's items (what it reads, what it lets HQ change) drawn as its part,
    ``more`` saying which hold words for the row's open line."""

    return tuple(
        Cell(piece=Piece(part, {key: item, "connection": row.connection}, label(item), more=more(item)))
        for item in items
    )


def _reaches(instance: Any) -> Cell:
    """The targets it reaches and the dependencies it names, every name a part of its own."""

    return Cell(
        parts=(
            *(Cell.of(declared_link(link.label, link.url)) for link in instance.targets),
            *(replace(Cell.of(declared_link(link.label, link.url)), readout=True) for link in instance.dependencies),
        ),
        fit=True,
    )


def _state(row: Any) -> Cell:
    """Its state in one word, and at most one plain line under it: since when,
    what is missing, or what happens next, then its open problems."""

    instance = row.connection.instance
    line = None
    if row.read_requested_at:
        line = Cell(f"Read asked {when(row.read_requested_at)}", muted=True, shows="narrow-more")
    elif row.state_line:
        line = Cell(
            row.state_line if not row.state_line_tone else "",
            status=StatusMark(row.state_line, tone=row.state_line_tone, pill=False) if row.state_line_tone else None,
            muted=True,
            shows="more" if row.state_line_quiet else "narrow-more",
        )
    elif row.changes_only and instance.observed_at:
        line = Cell(f"Checked {when(instance.observed_at)}", muted=True, shows="narrow-more")
    elif not (row.sight and row.sight.sights) and instance.observed_at:
        line = Cell(piece=reading_age(at=instance.observed_at), muted=True, shows="narrow-more")
    problems = (
        (
            Cell(
                status=StatusMark(counted(row.problems, "problem"), tone="attention", pill=False, href=row.problems_url)
            ),
        )
        if row.problems
        else ()
    )
    return Cell(
        status=StatusMark(row.state_label, tone=row.lifecycle.tone, dot=True),
        lines=(*((line,) if line else ()), *problems),
    )
