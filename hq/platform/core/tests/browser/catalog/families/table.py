"""The catalog's tables: a data table in each option and state its parts give it."""

from hq.platform.application.pages import PageAction
from hq.platform.core.tests.browser.catalog.common import Entry

ENTRIES = {
    # What the table region derives from its headings, each in a table of its
    # own: a figure's column aligned under its heading, body and totals, with
    # no class on a cell; the sort control on a table of rows and none on a
    # table of one; each row's toggle in the gutter column's cell, shown on a
    # row that holds something back.
    "table/figures": Entry(
        '{% section "Example costs" %}{% table_region "Example costs" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "Count" "numeric" %}{% table_heading "Cost" "numeric" %}'
        "</tr></thead><tbody><tr><td>example-01</td><td>3</td><td>$1,204.50</td></tr>"
        "<tr><td>example-02</td><td>12</td><td>$8.00</td></tr></tbody>"
        "<tfoot><tr><td>Total</td><td>15</td><td>$1,212.50</td></tr></tfoot></table>"
        "{% endtable_region %}{% endsection %}"
    ),
    "table/sorts": Entry(
        '{% section "Example hosts" %}{% table_region "Example hosts" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}'
        '{% table_heading "Actions" "control" hidden=True %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td><td><a href="#">Open</a></td></tr>'
        '<tr><td>example-02</td><td>{% status "Offline" tone="attention" %}</td><td><a href="#">Open</a></td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}"
        '{% section "Example host" %}{% table_region "Example host" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% status "Online" tone="good" %}</td></tr></tbody></table>'
        "{% endtable_region %}{% endsection %}"
    ),
    # A region whose tables hold no row: what is missing and what fills it,
    # then a list its own filter emptied, with the way back.
    "table/empty": Entry(
        '{% section "Example hosts" %}{% table_region "Example hosts" empty="No example hosts yet." '
        'action_url="#" action_label="Add an example host" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead><tbody></tbody></table>'
        "{% endtable_region %}{% endsection %}"
        '{% section "Example records" %}{% table_region "Example records" empty="No example records yet." '
        'filtered=True clear="#" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" %}{% table_heading "State" "status" %}</tr></thead><tbody></tbody></table>'
        "{% endtable_region %}{% endsection %}"
    ),
    "table/gutter": Entry(
        '{% section "Example records" %}{% table_region "Example records" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" gutter=True %}{% table_heading "Note" %}</tr></thead><tbody>'
        "<tr><td><strong>example-01</strong></td><td>An example note"
        '<div class="row-below">An example line the row holds back.</div></td></tr>'
        "<tr><td><strong>example-02</strong></td><td>Another example note</td></tr></tbody></table>"
        "{% endtable_region %}{% endsection %}"
    ),
    # A row's actions ({% row_actions %}): a POST and a destructive link side
    # by side, a link alone, one refused, and a row whose note takes several
    # lines, each set on its row's first line.
    "table/row-actions": Entry(
        '{% section "Example messages" %}{% table_region "Example messages" %}<table class="data-table"><thead><tr>'
        '{% table_heading "From" "identifier" gutter=True %}{% table_heading "Note" %}'
        '{% table_heading "Actions" css="actions-col" hidden=True drop=1 %}</tr></thead><tbody>'
        "{% for row in rows %}<tr><td><strong>{{ row.0 }}</strong></td><td>{{ row.1 }}</td>"
        "{% row_actions row.2 %}</tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "rows": (
                (
                    "example-01",
                    "An example note.",
                    (
                        PageAction("Read", "/example/1/status/", method="post", value="read", field="status"),
                        PageAction("Delete", "/example/1/delete/", danger=True),
                    ),
                ),
                ("example-02", "Another example note.", (PageAction("Open", "/example/2/"),)),
                (
                    "example-03",
                    "An example note that wraps on a phone.",
                    (PageAction("Spam", "/example/3/", method="post", disabled=True, title="An example reason."),),
                ),
            )
        },
    ),
    # Values set side by side ({% table_region compare=True %}): each row's
    # field, then its two values sharing the room evenly, one long enough to
    # wrap.
    "table/compare": Entry(
        '{% section "Example changes" %}{% table_region "Example changes" compare=True %}'
        '<table class="data-table compact-table"><thead><tr>{% table_heading "Field" "identifier" %}'
        '{% table_heading "Before" %}{% table_heading "After" %}</tr></thead><tbody>'
        "<tr><th>example_name</th><td>example-01</td><td>example-02</td></tr>"
        "<tr><th>example_note</th><td>An example note long enough to take two lines on a phone.</td>"
        "<td>Another example note.</td></tr></tbody></table>{% endtable_region %}{% endsection %}"
    ),
    # A row that asks to be read first ({% row_state emphasized=True %}) among
    # rows that do not, its links weighed and its cells tinted.
    "table/emphasized": Entry(
        '{% section "Example messages" %}{% table_region "Example messages" %}<table class="data-table"><thead><tr>'
        '{% table_heading "From" "identifier" gutter=True %}{% table_heading "Message" %}</tr></thead><tbody>'
        "{% for row in rows %}<tr{% row_state emphasized=row.2 %}><td>{{ row.0 }}</td>"
        '<td><a href="#">{{ row.1 }}</a></td></tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}',
        {"rows": (("example-01", "An example message not read yet.", True), ("example-02", "An example read.", False))},
    ),
    # A table whose rows are selected together: the heading that selects
    # every row, then each row's tick-box ({% row_select %}), and a row with
    # no key whose cell stays empty.
    "table/selectable": Entry(
        '{% section "Example assets" %}{% table_region "Example assets" %}'
        '<table class="data-table" data-selectable-table><thead><tr>'
        '{% table_heading "Select all rows" select_all=True %}{% table_heading "Name" "identifier" gutter=True %}'
        '{% table_heading "State" "status" %}</tr></thead><tbody>'
        "{% for row in rows %}<tr>{% row_select row.0 %}<td>{{ row.1 }}</td>"
        '<td>{% status "Online" tone="good" %}</td></tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}',
        {"rows": ((1, "example-01"), (2, "example-02"), (None, "example-03"))},
    ),
    # A run of parts in a cell ({% line_list %}): four that all show, two,
    # and nine. With script the parts past the first three show as far as
    # their line has room and the row opens for the rest; without it, as
    # here, the rest wrap on lines of their own beneath the first three.
    "table/line-list": Entry(
        '{% section "Example stacks" %}{% table_region "Example stacks" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" gutter=True %}{% table_heading "Parts" %}</tr></thead><tbody>'
        "{% for row in rows %}<tr><td>{{ row.0 }}</td><td>{% line_list row.1 as part %}{% status part chip=True %}"
        "{% endline_list %}</td></tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "rows": (
                ("example-01", ("Debian", "systemd", "Caddy", "Tailscale")),
                ("example-02", ("Django", "SQLite")),
                ("example-03", tuple(f"example-part-{at:02}" for at in range(1, 10))),
            )
        },
    ),
}
