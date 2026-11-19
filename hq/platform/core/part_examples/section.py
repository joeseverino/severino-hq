"""The section's examples: each way it heads, folds and holds, alone and in the flow."""

lazy from datetime import date

from hq.platform.core.part_examples import NARROW
from hq.platform.core.template_parts import Example
lazy from hq.platform.application.card_items import ListRow
lazy from hq.platform.application.drawings import AllocationBar, Share


def _flow(*sections):
    """A run of sections in the flow (`.section-flow`), each named and holding
    what it is given."""

    return (
        '<div class="section-flow">'
        + "".join(f'{{% section "{heading}" %}}{body}{{% endsection %}}' for heading, body in sections)
        + "</div>"
    )


# A wide section's body: a table of four columns, which takes the whole row.
_WIDE = (
    '{% table_region "Example hosts" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Name" %}{% table_heading "State" drop=3 %}{% table_heading "Address" drop=1 %}'
    '{% table_heading "Seen" drop=2 %}'
    "</tr></thead><tbody><tr><td>example-01</td><td>Online</td><td>192.0.2.10</td><td>Today</td></tr></tbody>"
    "</table>{% endtable_region %}"
)


SECTION_STATES = {
    # A table in a section of its own name, one that fits and one that
    # scrolls in a card of its own name, as a record page and a ledger draw
    # them: each name is one landmark on the page.
    "around-table": Example(
        '{% section "Example records" %}{% table_region "Example records" %}'
        '<table class="data-table"><tbody><tr><td>Example</td></tr></tbody></table>'
        "{% endtable_region %}{% endsection %}"
        '{% card heading="example-ledger" %}<h2 id="example-ledger">Example ledger</h2>'
        '{% table_region "Example ledger" pane=True %}'
        '<table class="data-table"><tbody><tr><td>Example</td></tr></tbody></table>'
        "{% endtable_region %}{% endcard %}"
    ),
    # Sibling sections in the flow: two, three and four narrow ones share a
    # row as far as the box holds them, and a wide one, written ahead of the
    # narrow ones, takes the row before they share theirs.
    "flow-two": Example(_flow(("Example identity", NARROW), ("Example link", NARROW))),
    "flow-three": Example(_flow(("Example identity 2", NARROW), ("Example link 2", NARROW), ("Example path", NARROW))),
    "flow-four": Example(
        _flow(
            ("Example identity 3", NARROW),
            ("Example link 3", NARROW),
            ("Example path 2", NARROW),
            ("Example device", NARROW),
        )
    ),
    "flow-wide": Example(_flow(("Example hosts", _WIDE), ("Example identity 4", NARROW), ("Example link 4", NARROW))),
    # A section that says how to read it beside a taller one: the row ends
    # both together, and the foot stands at the bottom of its frame.
    "flow-foot": Example(
        _flow(
            ("Example identity 5", NARROW + "{% foot %}Example of how to read it."),
            (
                "Example link 5",
                "{% facts %}"
                + "".join(f'{{% fact "Example fact {n}" value="example-{n:02}" %}}' for n in range(1, 7))
                + "{% endfacts %}",
            ),
        )
    ),
    # The section part ({% section %}) in each state a page can give it; a new
    # option of the tag comes with an entry here (test_catalog's source check).
    "bare": Example('{% section "Example notes 2" %}<p>An example line of prose in the frame.</p>{% endsection %}'),
    "full": Example(
        '{% section "An example heading long enough to need the row" count="12 example hosts" id="example-hosts"'
        ' css="example" data_example="example"'
        ' lede="An example lede, the line that says what the section holds." %}'
        "<p>An example line of prose in the frame.</p>"
        '{% aside %}<span class="pill tone-neutral">Example state</span>'
        ' <a class="btn ghost compact" href="/example/">Example action</a>{% endsection %}'
    ),
    "empty": Example(
        '{% section "Example agents" count=0 %}{% empty %}No example agent has connected yet.{% endsection %}'
    ),
    # An empty line with the link that fills it, as a record with no receipt draws it.
    "empty-remedy": Example(
        '{% section "Example receipts" remedy_label="Upload an example receipt" remedy_url="/example/new/" %}'
        "{% empty %}No example receipts.{% endsection %}"
    ),
    # A heading that is the moment it names, as a calendar's month is, with
    # its controls beside it; and a moment after the heading's words, as a
    # calendar's day is.
    "moment": Example(
        '{% section moment=month when="month" id="example-month" %}<p>An example line of prose in the frame.</p>'
        '{% aside %}<a class="btn ghost compact" href="/example/">Example action</a>{% endsection %}'
        '{% section "Example day" moment=month when="day" %}<p>An example line of prose in the frame.</p>'
        "{% endsection %}",
        lambda: {"month": date(2026, 11, 1)},
    ),
    # A table whose region carries the heading's name, as a list's sections draw one.
    "table": Example(
        '{% section "Example hosts 2" count=2 %}{% table_region "Example hosts 2" %}'
        '<table class="data-table"><thead><tr><th scope="col">Name</th><th scope="col">State</th></tr></thead>'
        "<tbody><tr><td>example-01</td><td>Online</td></tr><tr><td>example-02</td><td>Online</td></tr></tbody>"
        "</table>{% endtable_region %}{% aside %}<span>Example source</span>{% endsection %}"
    ),
    # A band of facts, which draws its own cells, fills the frame as a table
    # does, the prose after it in the frame's padding; as a project's GitHub
    # panel draws one.
    "band": Example(
        '{% section "Example repository" %}{% facts layout="band" %}'
        '{% fact "Example commit" value="Example" detail="An example detail" icon="commit" %}'
        '{% fact "Example checks" value="Passing" detail="An example detail" icon="check-circle" tone="good" %}'
        '{% fact "Example alerts" value="Clear" icon="shield" %}'
        "{% endfacts %}<p>An example line after the band.</p>{% endsection %}"
    ),
    # A section that stands out while in a state, its tone down its frame's
    # starting edge: framed whole as a held request's card is, and framed by
    # its body under a head that stands above it.
    "edge": Example(
        '{% section "Example request" edge="attention" lede="An example agent wants to change an example record." '
        'more_label="example.com" more_url="/example/" %}<p>An example line of prose in the frame.</p>'
        '{% endsection %}{% section "Example hosts 3" edge="serious" %}{% table_region "Example hosts 3" %}'
        '<table class="data-table"><thead><tr><th scope="col">Name</th></tr></thead>'
        "<tbody><tr><td>example-01</td></tr></tbody></table>{% endtable_region %}{% endsection %}"
    ),
    "fold": Example(
        '{% section "Example readings" count=3 fold=True lede="An example lede of a folded section."'
        " %}<p>An example line behind the fold.</p>{% aside %}<span>Example source</span>{% endsection %}"
    ),
    "fold-open": Example(
        '{% section "Example names" fold=True open=True %}{% table_region "Example names" %}'
        '<table class="data-table"><thead><tr><th scope="col">Name</th></tr></thead>'
        "<tbody><tr><td>example.com</td></tr></tbody></table>{% endtable_region %}{% endsection %}"
    ),
    "head-above": Example('{% section "Example" head="above" %}<p>An example line.</p>{% endsection %}'),
    "head-inside": Example(
        '{% section "Example 2" head="inside" %}{% table_region "Example 2" %}<table class="data-table"><thead><tr>'
        "<th>Name</th></tr></thead><tbody><tr><td>Example</td></tr></tbody></table>{% endtable_region %}"
        "{% endsection %}"
    ),
    # A line under a body that says how to read it, after a table that would
    # otherwise end the frame, and the aside's link to the whole, after a
    # line of its own and alone.
    "foot": Example(
        '{% section "Example trends" more_label="All example trends" more_url="/example/" %}'
        '{% table_region "Example trends" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Example" "identifier" %}{% table_heading "Slope" "numeric" %}</tr></thead>'
        "<tbody><tr><td>Example pace</td><td>0.4</td></tr></tbody></table>{% endtable_region %}"
        '{% aside %}<span class="count">2 example weeks</span>'
        "{% foot %}R² is how much of the example movement the fitted line explains. It does not show cause."
        "{% endsection %}"
    ),
    "more": Example(
        '{% section "Example renewals" count=1 more_label="View example renewals" more_url="/example/" %}'
        "<p>An example line of prose in the frame.</p>{% endsection %}"
    ),
    # A drawing as a figure, its head the caption: the heading, the line that
    # says what it shows, and its legend beside them.
    "figure": Example(
        '{% section "Example allocation" figure=True lede="Each example account as its share of the whole." %}'
        '{% include "partials/_allocation.html" %}'
        '{% aside %}<span class="count">2 example accounts</span>{% endsection %}',
        lambda: {"allocation": AllocationBar("Example allocation", (Share("Cash", 3, 1), Share("Owed", 1, 2)))},
    ),
    "fold-empty": Example(
        '{% section "Example dismissed" fold=True open=True %}{% empty %}Nothing example is dismissed.{% endsection %}'
    ),
    # Sections in a section, as a project's GitHub panel draws them: each inner
    # head quieter than the outer one, and each spaced from what is above it
    # by the stack gap, after prose and after a list of rows alike.
    "nested-rows": Example(
        '{% section "Example repository 2" %}<p>An example line about the repository.</p>'
        '{% section "Example pull requests" count=2 %}{% include "partials/_record_list.html" %}{% endsection %}'
        '{% section "Example workflows" %}<p>Example build, example test</p>{% endsection %}'
        "{% endsection %}",
        lambda: {
            "rows": [
                ListRow("Example change", url="/example/41/", detail="#41"),
                ListRow("Another example change", url="/example/42/", detail="#42", badge="Draft"),
            ]
        },
    ),
    # A section in a section: the inner head in the outer frame, the inner
    # table reaching the outer frame's sides and foot.
    "nested": Example(
        '{% section "Example path 3" %}<p>An example line before the steps.</p>'
        '{% section "Example steps" %}{% table_region "Example steps" %}'
        '<table class="data-table"><thead><tr><th scope="col">Step</th><th scope="col">State</th></tr></thead>'
        "<tbody><tr><td>Example DNS</td><td>Working</td></tr></tbody></table>{% endtable_region %}{% endsection %}"
        "{% endsection %}"
    ),
    # A section inside a card, as a topology node or a held request's preview
    # draws it: part of the card's frame, its head with no rule or inset.
    "in-card": Example(
        "{% card %}<p>An example line about the card.</p>"
        '{% section "Example preview" count=2 %}<p>An example line in the inner section.</p>'
        '{% aside %}<span class="pill tone-neutral">Example state</span>{% endsection %}{% endcard %}'
    ),
    # A reading as a folded section, open: its long lines wrap in the frame and
    # only its height scrolls.
    "readout": Example(
        '{% section "Example readings" %}{% section "Example reading" fold=True open=True %}'
        '<pre tabindex="0">{"example_key_with_a_long_name": "an-example-value-long-enough-to-wrap-at-a-phone-width",'
        ' "another_example_key": "another-example-value"}</pre>{% endsection %}{% endsection %}'
    ),
    # A link onward whose words wrap at a phone's width: its arrow stays with
    # its last word.
    "more-wrapped": Example(
        '{% section "Example request 2" more_label="example-service-with-a-deliberately-long-name.example.com"'
        ' more_url="/example/" %}<p>An example line of prose in the frame.</p>{% endsection %}'
    ),
}
