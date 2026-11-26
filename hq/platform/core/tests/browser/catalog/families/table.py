"""The catalog's tables: a data table in each option and state its parts give it."""

from datetime import date

from django import forms

from hq.platform.application.pages import PageAction
from hq.platform.application.tables import Below, Cell, DataTable, Group, Piece, Row, TableColumn
from hq.platform.core import included_parts
from hq.platform.core.part_examples import runs, table
from hq.platform.core.template_parts import Example

# Each drawn heading of a table of the page named ``label``: the premise of an
# entry whose column of that name holds nothing, so is drawn nowhere.
_HEADINGS_DRAWN = """(label) => [...document.querySelectorAll('main thead th')]
  .filter((heading) => heading.checkVisibility() && heading.textContent.trim() === label)
  .map((heading) => `the column "${label}" is drawn, though it holds nothing`)"""


def _sectioned(**tables: str) -> str:
    """Each table, by its name in the context, in a section of its label."""

    return "".join(
        f'{{% section "{label}" %}}{{% data_table {name} %}}{{% endsection %}}' for name, label in tables.items()
    )


def _hosts(label: str, *rows: tuple[str, str, str], actions: bool = False) -> DataTable:
    columns = (TableColumn("Name"), TableColumn("State", kind="status"))
    return DataTable(
        label,
        columns,
        tuple(
            Row(
                (Cell(name), Cell(state, tone=tone)),
                actions=(PageAction("Open", f"/example/{name}/"),) if actions else (),
            )
            for name, state, tone in rows
        ),
    )


def _row_actions() -> DataTable:
    rows = (
        (
            "example-01",
            "An example note.",
            (
                PageAction._built(label="Read", url="/example/1/status/", method="post", value="read", field="status"),
                PageAction._built(label="Delete", url="/example/1/delete/", danger=True),
            ),
        ),
        ("example-02", "Another example note.", (PageAction("Open", "/example/2/"),)),
        (
            "example-03",
            "An example note that wraps on a phone.",
            (
                PageAction._built(
                    label="Spam", url="/example/3/", method="post", disabled=True, title="An example reason."
                ),
            ),
        ),
    )
    return DataTable(
        "Example messages",
        (TableColumn("From", kind="identifier"), TableColumn("Note")),
        tuple(Row((Cell(name), Cell(note)), actions=actions) for name, note, actions in rows),
    )


def _many_actions() -> DataTable:
    rows = tuple(
        Row(
            (
                Cell(
                    f"example-{at:02} was started outside its compose project",
                    lines=(Cell("An example note long enough to wrap over several lines beside the row's actions."),),
                ),
            ),
            actions=(
                PageAction("Mark as intended", f"/example/{at}/edit/"),
                PageAction("Open containers", "/example/containers/"),
                PageAction._built(label="Restart", url=f"/example/{at}/restart/", method="post"),
            ),
        )
        for at in (1, 2)
    )
    return DataTable("Example findings", (TableColumn("Finding"),), rows, headed=False)


def _controls_per_row() -> DataTable:
    """Two folds whose rows' actions differ in length, as the queue's families do."""

    groups = tuple(
        Group(
            label,
            (
                Row(
                    (Cell(f"An example {fold.split('-')[0]} item"),),
                    actions=(PageAction(action, f"/example/{fold}/"),),
                    folded=fold,
                ),
            ),
            folds=fold,
        )
        for label, fold, action in (
            ("Short actions", "short-actions", "Review"),
            ("Long actions", "long-actions", "Check for active reboot inhibitors"),
        )
    )
    return DataTable(
        "Example queue 2",
        (TableColumn("Item"),),
        groups=groups,
        headed=False,
        data={"line": "flat"},
        controls_per_row=True,
    )


def _identifiers() -> dict[str, DataTable]:
    address = "203.0.113.214:8000"
    up = Cell("Up", tone="good")
    steps = table(
        "Example steps",
        (
            "Step",
            TableColumn("Name", kind="identifier"),
            TableColumn("State", kind="status"),
            TableColumn("Since", drop=3),
            TableColumn("Impact", drop=2),
            TableColumn("Read from", drop=1),
        ),
        (
            "Proxy host",
            Cell(address, url="/example/"),
            up,
            "1 day",
            "If it is removed, the name stops reaching the app it serves.",
            "Read from the proxy's own list of hosts, a minute ago",
        ),
        (
            "Forwards to",
            address,
            up,
            "1 day",
            "If nothing listens there, a reader gets a gateway error.",
            "Read from the container's ports",
        ),
        ("Service", "status.example.com", "", "1 day", "The service stops answering at this name.", "Declared in HQ"),
    )
    access = table(
        "Example access",
        (
            "Access list",
            TableColumn("Admits by", drop=3),
            TableColumn("Addresses", drop=2),
            TableColumn("Logins", drop=1),
        ),
        ("office only", "Address rules", "allow 198.51.100.0/24; allow 2001:db8:a1e0::/48", "none"),
    )
    return {"steps": steps, "access": access}


def _crowded() -> DataTable:
    """Nine columns, as Services draws: short identifiers beside long ones and a sentence."""

    def chip(value: str) -> Cell:
        return Cell(value, readout=True)

    return table(
        "Example services",
        (
            TableColumn("Service", kind="identifier"),
            "Open to",
            "Address",
            "Proxy",
            "Certificate",
            "Runs on",
            "Note",
            TableColumn("State", kind="status"),
            TableColumn("Since", kind="status"),
        ),
        *(
            (
                Cell(name),
                "Tailnet only",
                chip(address),
                chip(f"{address}:{port}"),
                chip("wildcard-example-com"),
                "example-host-01 · example-app",
                "Served through the proxy with a certificate from the example authority.",
                Cell("Health check passing", tone="good"),
                "1 day",
            )
            for name, address, port in (
                ("home-automation-dashboard.example.com", "198.51.100.214", "8000"),
                ("status.example.com", "198.51.100.215", "8443"),
            )
        ),
    )


def _choices() -> DataTable:
    """A column of choices per agent, as the agent rules draw: a select in
    each row of one, the words that stand where an agent holds no grant in
    the other, beside a day each row keeps, a note it can spare and a row's
    own control."""

    form = forms.Form()
    rows = []
    for at, (name, day) in enumerate((("Edit an example record", 3), ("Stop an example container", 12)), 1):
        form.fields[f"rule-{at}"] = forms.ChoiceField(
            label=f"{name}, for example-agent",
            choices=[("", "Default"), ("approve", "Ask me first"), ("deny", "Off")],
            required=False,
        )
        rows.append(
            Row(
                (
                    Cell(name),
                    Cell("Ask me first", muted=True),
                    Cell(moment=date(2026, 3, day)),
                    Cell("A note a narrow box lets go to the row's open line."),
                    Cell(field=form[f"rule-{at}"]),
                    Cell("Not granted", muted=True),
                ),
                actions=(PageAction("Open", f"/example/{at}/"),) if at == 1 else (),
            )
        )
    columns = (
        TableColumn("Action"),
        TableColumn("Default", css="key-col"),
        TableColumn("Changed"),
        TableColumn("Note"),
        TableColumn("example-agent", kind="control"),
        TableColumn("example-reader", kind="control"),
    )
    return DataTable("Example rules", columns, tuple(rows))


def _blank_column() -> DataTable:
    def ran() -> Cell:
        return Cell(piece=Piece(included_parts.DOTS, {"dots": runs()}, words=""))

    return table(
        "Example runbooks",
        ("Title", "Type", "Nothing", "Runs", "Note"),
        (
            "Upgrade an example container to a new release",
            "Runbook",
            "",
            ran(),
            "A sentence long enough to take the room it is given and wrap.",
        ),
        ("Rotate the example signing keys", "Runbook", "–", ran(), "Another sentence that wraps in its column."),
    )


ENTRIES = {
    # What a declared table derives from its headings, each in a table of its
    # own: a figure's column aligned under its heading, body and totals; the
    # sort control on a table of rows and none on a table of one; each row's
    # toggle in the cell that names it, shown on a row that holds something back.
    "table/figures": Example(
        _sectioned(costs="Example costs"),
        lambda: {
            "costs": table(
                "Example costs",
                ("Name", TableColumn("Count", kind="numeric"), TableColumn("Cost", kind="numeric")),
                ("example-01", "3", "$1,204.50"),
                ("example-02", "12", "$8.00"),
                footer=Row((Cell("Total"), Cell("15"), Cell("$1,212.50"))),
            )
        },
    ),
    "table/sorts": Example(
        _sectioned(hosts="Example hosts 5", host="Example host 3"),
        lambda: {
            "hosts": _hosts(
                "Example hosts 5",
                ("example-01", "Online", "good"),
                ("example-02", "Offline", "attention"),
                actions=True,
            ),
            "host": _hosts("Example host 3", ("example-01", "Online", "good")),
        },
    ),
    # Tables that hold no row: what is missing and what fills it, then a list
    # its own filter emptied, with the way back.
    "table/empty": Example(
        _sectioned(hosts="Example hosts 6", records="Example records 4"),
        lambda: {
            "hosts": table(
                "Example hosts 6",
                ("Name", TableColumn("State", kind="status")),
                region={"empty": "No example hosts yet.", "action_url": "#", "action_label": "Add an example host"},
            ),
            "records": table(
                "Example records 4",
                ("Name", TableColumn("State", kind="status")),
                region={"empty": "No example records yet.", "filtered": True, "clear": "#"},
            ),
        },
    ),
    "table/gutter": Example(
        _sectioned(records="Example records 5"),
        lambda: {
            "records": table(
                "Example records 5",
                (TableColumn("Name", kind="identifier"), "Note"),
                (
                    Cell("example-01"),
                    Cell("An example note", below=(Below("", (Cell("An example line the row holds back."),)),)),
                ),
                (Cell("example-02"), "Another example note"),
            )
        },
    ),
    # A fact on one line (``Cell(cut=True)``), as Jobs says who asked for
    # each run; a cut of one is measured, and opened, by tables.js.
    "table/one-line": Example(
        _sectioned(runs="Example runs 2"),
        lambda: {
            "runs": table(
                "Example runs 2",
                (TableColumn("Run", kind="identifier"), "Asked by"),
                (Cell("example-01"), Cell("example-owner, on the web", cut=True)),
                (Cell("example-02"), Cell("example-agent, over MCP", cut=True)),
            )
        },
    ),
    # A row's actions: a POST and a destructive link side by side, a link
    # alone, one refused, and a row whose note takes several lines, each set
    # on its row's first line.
    "table/row-actions": Example(_sectioned(messages="Example messages"), lambda: {"messages": _row_actions()}),
    # A table with no headings, as the action queue draws, whose rows each
    # offer several actions with long labels beside words that wrap: the
    # actions keep their line while the table has room and wrap between
    # them where it has not.
    "table/many-actions": Example(_sectioned(findings="Example findings"), lambda: {"findings": _many_actions()}),
    # A queue's folds, each row's controls their own width
    # (``controls_per_row``): opening one fold moves no row of another.
    "table/controls-per-row": Example(_sectioned(queue="Example queue 2"), lambda: {"queue": _controls_per_row()}),
    # A table whose rows are selected together: the heading that selects
    # every row, then each row's tick-box, and a row with no key whose cell
    # stays empty.
    "table/selectable": Example(
        _sectioned(assets="Example assets"),
        lambda: {
            "assets": DataTable(
                "Example assets",
                (TableColumn("Name", kind="identifier"), TableColumn("State", kind="status")),
                tuple(
                    Row((Cell(name), Cell("Online", tone="good")), key=key)
                    for key, name in ((1, "example-01"), (2, "example-02"), (None, "example-03"))
                ),
                selectable=True,
            )
        },
    ),
    # Identifiers beside sentences, as a service's path draws its steps: an
    # address with a port, a host name and a list of networks, in tables with
    # ranked columns to let go, so a narrow box lets a column leave before it
    # breaks a name at its seams.
    "table/identifiers": Example(_sectioned(steps="Example steps", access="Example access"), _identifiers),
    # A table of nine columns, short identifiers (an address and its port, a
    # certificate's name) beside a long host name and a sentence: each short
    # one is whole while a long one or the sentence still has room to give.
    "table/crowded": Example(_sectioned(services="Example services"), lambda: {"services": _crowded()}),
    # A column of choices per agent: in a narrow box each row's choices stand
    # each on a line of its own beneath its name, named by its column, and
    # its day stands while its note leaves.
    "table/choices": Example(_sectioned(rules="Example rules"), lambda: {"rules": _choices()}),
    # A column of dashes beside a column of marks with no words and a column
    # of short words: the server marks the dashes' column blank and it is not
    # drawn, the marks are, and no short word is split however its column's
    # width rounds.
    "table/blank-column": Example(
        _sectioned(runbooks="Example runbooks"),
        lambda: {"runbooks": _blank_column()},
        premise=(_HEADINGS_DRAWN, "Nothing"),
    ),
}
