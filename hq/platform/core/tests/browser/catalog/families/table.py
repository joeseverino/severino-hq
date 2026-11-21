"""The catalog's tables: a data table in each option and state its parts give it."""

from hq.platform.application.pages import PageAction
from hq.platform.core.template_parts import Example

# Each drawn heading of a table of the page named ``label``: the premise of an
# entry whose column of that name holds nothing, so is drawn nowhere.
_HEADINGS_DRAWN = """(label) => [...document.querySelectorAll('main thead th')]
  .filter((heading) => heading.checkVisibility() && heading.textContent.trim() === label)
  .map((heading) => `the column "${label}" is drawn, though it holds nothing`)"""

ENTRIES = {
    # What the table region derives from its headings, each in a table of its
    # own: a figure's column aligned under its heading, body and totals, with
    # no class on a cell; the sort control on a table of rows and none on a
    # table of one; each row's toggle in the cell that names it, shown on a
    # row that holds something back.
    "table/figures": Example(
        '{% section "Example costs" %}{% table_region "Example costs" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "Count" "numeric" %}{% table_heading "Cost" "numeric" %}'
        "</tr></thead><tbody><tr><td>example-01</td><td>3</td><td>$1,204.50</td></tr>"
        "<tr><td>example-02</td><td>12</td><td>$8.00</td></tr></tbody>"
        "<tfoot><tr><td>Total</td><td>15</td><td>$1,212.50</td></tr></tfoot></table>"
        "{% endtable_region %}{% endsection %}"
    ),
    "table/sorts": Example(
        '{% section "Example hosts 5" %}{% table_region "Example hosts 5" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}'
        '{% table_heading "Actions" "control" hidden=True %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td><td><a href="#">Open</a></td></tr>'
        '<tr><td>example-02</td><td>{% status "Offline" tone="attention" %}</td><td><a href="#">Open</a></td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}"
        '{% section "Example host 3" %}{% table_region "Example host 3" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td></tr></tbody></table>'
        "{% endtable_region %}{% endsection %}"
    ),
    # A region whose tables hold no row: what is missing and what fills it,
    # then a list its own filter emptied, with the way back.
    "table/empty": Example(
        '{% section "Example hosts 6" %}{% table_region "Example hosts 6" empty="No example hosts yet." '
        'action_url="#" action_label="Add an example host" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead><tbody></tbody></table>'
        "{% endtable_region %}{% endsection %}"
        '{% section "Example records 4" %}{% table_region "Example records 4" empty="No example records yet." '
        'filtered=True clear="#" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead><tbody></tbody></table>'
        "{% endtable_region %}{% endsection %}"
    ),
    "table/gutter": Example(
        '{% section "Example records 5" %}{% table_region "Example records 5" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" %}{% table_heading "Note" %}</tr></thead><tbody>'
        "<tr><td><strong>example-01</strong></td><td>An example note"
        '<div class="row-below">An example line the row holds back.</div></td></tr>'
        "<tr><td><strong>example-02</strong></td><td>Another example note</td></tr></tbody></table>"
        "{% endtable_region %}{% endsection %}"
    ),
    # A fact on one line ({% cell_line text=... lines=1 %}), as Jobs says who asked for
    # each run; a cut of one is measured, and opened, by tables.js.
    "table/one-line": Example(
        '{% section "Example runs 2" %}{% table_region "Example runs 2" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Run" "identifier" %}{% table_heading "Asked by" %}</tr></thead><tbody>'
        '<tr><td><strong>example-01</strong></td><td>{% cell_line text="example-owner, on the web" lines=1 %}</td></tr>'
        '<tr><td><strong>example-02</strong></td><td>{% cell_line text="example-agent, over MCP" lines=1 %}</td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}"
    ),
    # A row's actions ({% row_actions %}): a POST and a destructive link side
    # by side, a link alone, one refused, and a row whose note takes several
    # lines, each set on its row's first line.
    "table/row-actions": Example(
        '{% section "Example messages" %}{% table_region "Example messages" %}<table class="data-table"><thead><tr>'
        '{% table_heading "From" "identifier" %}{% table_heading "Note" %}'
        '{% table_heading "Actions" css="actions-col" hidden=True %}</tr></thead><tbody>'
        "{% for row in rows %}<tr><td><strong>{{ row.0 }}</strong></td><td>{{ row.1 }}</td>"
        "{% row_actions row.2 %}</tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "rows": (
                (
                    "example-01",
                    "An example note.",
                    (
                        PageAction._built(
                            label="Read", url="/example/1/status/", method="post", value="read", field="status"
                        ),
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
        },
    ),
    # A table with no headings, as the action queue draws, whose rows each
    # offer several actions with long labels beside words that wrap: the
    # actions keep their line while the region has room and wrap between
    # them where it has not.
    "table/many-actions": Example(
        '{% section "Example findings" %}{% table_region "Example findings" %}<table class="data-table"><tbody>'
        "{% for row in rows %}<tr><td><strong>{{ row.0 }}</strong><p>{{ row.1 }}</p></td>"
        "{% row_actions row.2 %}</tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "rows": tuple(
                (
                    f"example-{at:02} was started outside its compose project",
                    "An example note long enough to wrap over several lines beside the row's actions.",
                    (
                        PageAction("Mark as intended", f"/example/{at}/edit/"),
                        PageAction("Open containers", "/example/containers/"),
                        PageAction._built(label="Check again", url=f"/example/{at}/check/", method="post"),
                    ),
                )
                for at in (1, 2)
            )
        },
    ),
    # Values set side by side ({% table_region compare=True %}): each row's
    # field, then its two values sharing the room evenly, one long enough to
    # wrap.
    "table/compare": Example(
        '{% section "Example changes 2" %}{% table_region "Example changes 2" compare=True %}'
        '<table class="data-table"><thead><tr>{% table_heading "Field" "identifier" %}'
        '{% table_heading "Before" %}{% table_heading "After" %}</tr></thead><tbody>'
        "<tr><th>example_name</th><td>example-01</td><td>example-02</td></tr>"
        "<tr><th>example_note</th><td>An example note long enough to take two lines on a phone.</td>"
        "<td>Another example note.</td></tr></tbody></table>{% endtable_region %}{% endsection %}"
    ),
    # A table whose rows are selected together: the heading that selects
    # every row, then each row's tick-box ({% row_select %}), and a row with
    # no key whose cell stays empty.
    "table/selectable": Example(
        '{% section "Example assets" %}{% table_region "Example assets" %}'
        '<table class="data-table" data-selectable-table><thead><tr>'
        '{% table_heading "Select all rows" select_all=True %}{% table_heading "Name" "identifier" %}'
        '{% table_heading "State" "status" %}</tr></thead><tbody>'
        "{% for row in rows %}<tr>{% row_select row.0 %}<td>{{ row.1 }}</td>"
        '<td>{% status "Online" tone="good" %}</td></tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}',
        {"rows": ((1, "example-01"), (2, "example-02"), (None, "example-03"))},
    ),
    # Identifiers beside sentences, as a service's path draws its steps: an
    # address with a port, a host name and a list of networks, in tables with
    # ranked columns to let go, so a narrow box lets a column leave before it
    # breaks a name at its seams.
    "table/identifiers": Example(
        '{% section "Example steps" %}{% table_region "Example steps" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Step" %}{% table_heading "Name" "identifier" %}'
        '{% table_heading "State" "status" %}{% table_heading "Since" drop=3 %}{% table_heading "Impact" drop=2 %}'
        '{% table_heading "Read from" drop=1 %}</tr></thead><tbody>'
        '<tr><td>Proxy host</td><td><a href="/example/" data-entity="Proxy host">{{ address }}</a></td>'
        '<td>{% status "Up" tone="good" %}</td><td>1 day</td>'
        "<td>If it is removed, the name stops reaching the app it serves.</td>"
        "<td>Read from the proxy's own list of hosts, a minute ago</td></tr>"
        '<tr><td>Forwards to</td><td>{{ address }}</td><td>{% status "Up" tone="good" %}</td>'
        "<td>1 day</td><td>If nothing listens there, a reader gets a gateway error.</td>"
        "<td>Read from the container's ports</td></tr>"
        '<tr><td>Service</td><td><span data-entity="Service">{{ host }}</span></td><td></td>'
        "<td>1 day</td><td>The service stops answering at this name.</td><td>Declared in HQ</td></tr>"
        "</tbody></table>{% endtable_region %}{% endsection %}"
        '{% section "Example access" %}{% table_region "Example access" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Access list" %}{% table_heading "Admits by" drop=3 %}'
        '{% table_heading "Addresses" drop=2 %}{% table_heading "Logins" drop=1 %}</tr></thead><tbody>'
        "<tr><td>office only</td><td>Address rules</td><td>{{ networks }}</td>"
        "<td>{{ logins }}</td></tr></tbody></table>{% endtable_region %}{% endsection %}",
        {
            "address": "203.0.113.214:8000",
            "host": "status.example.com",
            "networks": "allow 198.51.100.0/24; allow 2001:db8:a1e0::/48",
            "logins": "none",
        },
    ),
    # A column of dashes beside a column of marks with no words and a column
    # of short words: the server marks the dashes' column blank and it is not
    # drawn, the marks are, and no short word is split however its column's
    # width rounds.
    "table/blank-column": Example(
        '{% section "Example runbooks" %}{% table_region "Example runbooks" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Title" %}{% table_heading "Type" %}{% table_heading "Nothing" %}'
        '{% table_heading "Runs" %}{% table_heading "Note" %}</tr></thead><tbody>'
        "<tr><td>Upgrade an example container to a new release</td><td>Runbook</td><td>{{ missing|or_empty }}</td>"
        '<td><span class="figure-dots"><span class="figure-dot is-filled" title="Succeeded"></span></span></td>'
        "<td>A sentence long enough to take the room it is given and wrap.</td></tr>"
        "<tr><td>Rotate the example signing keys</td><td>Runbook</td><td>–</td>"
        '<td><span class="figure-dots"><span class="figure-dot is-filled" title="Succeeded"></span></span></td>'
        "<td>Another sentence that wraps in its column.</td></tr>"
        "</tbody></table>{% endtable_region %}{% endsection %}",
        {"missing": None},
        premise=(_HEADINGS_DRAWN, "Nothing"),
    ),
}
