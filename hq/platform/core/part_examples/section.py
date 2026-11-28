"""The section's examples: each way it heads, folds and holds."""

lazy from datetime import date

from hq.platform.core.part_examples import table
from hq.platform.core.template_parts import Example
lazy from hq.platform.application.card_items import Badge, ListRow, RowTitle
lazy from hq.platform.application.drawings import AllocationBar, Share
lazy from hq.platform.application.tables import TableColumn

SECTION_STATES = {
    # Folds drawn as one line inside a box, one closed and one the server drew open.
    "line": Example(
        '{% card label="Example lines" %}'
        '{% section "Example checks" fold=True line=True %}<p>An example check.</p>{% aside %}2 of 3 passed{% endsection %}'
        '{% section "Example apps" fold=True line=True open=True %}<p>An example app.</p>{% aside %}1 failing{% endsection %}'
        "{% endcard %}"
    ),
    # A table in a section of its own name, one that fits and one that
    # scrolls in a card of its own name, as a record page and a ledger draw
    # them: each name is one landmark on the page.
    "around-table": Example(
        '{% section "Example records" %}{% data_table records %}{% endsection %}'
        '{% card heading="example-ledger" %}<h2 id="example-ledger">Example ledger</h2>'
        "{% data_table ledger %}{% endcard %}",
        lambda: {
            "records": table("Example records", ("Name",), ("Example",), headed=False),
            "ledger": table("Example ledger", ("Name",), ("Example",), headed=False, region={"pane": True}),
        },
    ),
    # The section part ({% section %}) in each state a page can give it; a new
    # option of the tag comes with an entry here (test_catalog's source check).
    "bare": Example('{% section "Example notes 2" %}<p>An example line of prose in the frame.</p>{% endsection %}'),
    "full": Example(
        '{% section "An example heading long enough to need the row" counts=hosts noun="host" id="example-hosts"'
        ' css="example" data_example="example"'
        ' lede="An example lede, the line that says what the section holds." %}'
        "<p>{{ hosts|join:', ' }}</p>"
        '{% aside %}<span class="pill tone-neutral">Example state</span>'
        ' <a class="btn ghost compact" href="/example/">Example action</a>{% endsection %}',
        {"hosts": tuple(f"example-{number:02}" for number in range(1, 13))},
    ),
    "empty": Example(
        '{% section "Example agents" counts=agents %}{% empty %}No example agent has connected yet.{% endsection %}',
        {"agents": ()},
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
        '{% section "Example hosts 2" counts=hosts.things %}{% data_table hosts %}{% aside %}<span>Example source</span>'
        "{% endsection %}",
        lambda: {
            "hosts": table("Example hosts 2", ("Name", "State"), ("example-01", "Online"), ("example-02", "Online"))
        },
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
        '{% endsection %}{% section "Example hosts 3" edge="serious" %}{% data_table hosts %}{% endsection %}',
        lambda: {"hosts": table("Example hosts 3", ("Name",), ("example-01",))},
    ),
    "fold": Example(
        '{% section "Example readings" counts=readings fold=True lede="An example lede of a folded section."'
        " %}{% for reading in readings %}<p>{{ reading }}</p>{% endfor %}{% aside %}<span>Example source</span>"
        "{% endsection %}",
        {"readings": ("An example reading.", "Another example reading.", "A third example reading.")},
    ),
    "fold-open": Example(
        '{% section "Example names" fold=True open=True %}{% data_table names %}{% endsection %}',
        lambda: {"names": table("Example names", ("Name",), ("example.com",))},
    ),
    "head-above": Example('{% section "Example" head="above" %}<p>An example line.</p>{% endsection %}'),
    "head-inside": Example(
        '{% section "Example 2" head="inside" %}{% data_table names %}{% endsection %}',
        lambda: {"names": table("Example 2", ("Name",), ("Example",))},
    ),
    # A line under a body that says how to read it, after a table that would
    # otherwise end the frame, and the aside's link to the whole, after a
    # line of its own and alone.
    "foot": Example(
        '{% section "Example trends" more_label="All example trends" more_url="/example/" %}'
        "{% data_table trends %}"
        "{% aside %}2 example weeks"
        "{% foot %}R² is how much of the example movement the fitted line explains. It does not show cause."
        "{% endsection %}",
        lambda: {
            "trends": table(
                "Example trends",
                (TableColumn("Example", kind="identifier"), TableColumn("Slope", kind="numeric")),
                ("Example pace", "0.4"),
            )
        },
    ),
    "more": Example(
        '{% section "Example renewals" counts=renewals more_label="View example renewals" more_url="/example/" %}'
        "{% for renewal in renewals %}<p>{{ renewal }}</p>{% endfor %}{% endsection %}",
        {"renewals": ("An example renewal.",)},
    ),
    # A drawing as a figure, its head the caption: the heading, the line that
    # says what it shows, and its legend beside them.
    "figure": Example(
        '{% section "Example allocation" figure=True lede="Each example account as its share of the whole." %}'
        '{% include "partials/_allocation.html" %}'
        "{% aside %}2 example accounts{% endsection %}",
        lambda: {"allocation": AllocationBar("Example allocation", (Share("Cash", 3, 1), Share("Owed", 1, 2)))},
    ),
    # Sections down a run of columns, as the map's lanes run, each body's
    # first line its lead, kept with its head wherever a column ends.
    "lead": Example(
        '<div style="columns: 12rem 2; max-inline-size: 26rem">'
        '{% section "Example lane" counts=lane %}{% for name in lane|slice:"1:" %}<p>{{ name }}</p>{% endfor %}'
        "{% lead %}<p>{{ lane.0 }}</p>{% endsection %}"
        '{% section "Another example lane" counts=other %}<p>{{ other.1 }}</p>'
        "{% lead %}<p>{{ other.0 }}</p>{% endsection %}</div>",
        {
            "lane": ("Example one", "Example two", "Example three", "Example four"),
            "other": ("Example five", "Example six"),
        },
    ),
    "fold-empty": Example(
        '{% section "Example dismissed" fold=True open=True %}{% empty %}Nothing example is dismissed.{% endsection %}'
    ),
    # Sections in a section, as a project's GitHub panel draws them: each inner
    # head quieter than the outer one, and each spaced from what is above it
    # by the stack gap, after prose and after a list of rows alike.
    "nested-rows": Example(
        '{% section "Example repository 2" %}<p>An example line about the repository.</p>'
        '{% section "Example pull requests" counts=rows %}{% include "partials/_record_list.html" %}{% endsection %}'
        '{% section "Example workflows" %}<p>Example build, example test</p>{% endsection %}'
        "{% endsection %}",
        lambda: {
            "rows": [
                ListRow(RowTitle("Example change", "/example/41/"), detail="#41"),
                ListRow(RowTitle("Another example change", "/example/42/"), detail="#42", state=Badge("Draft")),
            ]
        },
    ),
    # A section in a section: the inner head in the outer frame, the inner
    # table reaching the outer frame's sides and foot.
    "nested": Example(
        '{% section "Example path 3" %}<p>An example line before the steps.</p>'
        '{% section "Example steps" %}{% data_table steps %}{% endsection %}'
        "{% endsection %}",
        lambda: {"steps": table("Example steps", ("Step", "State"), ("Example DNS", "Working"))},
    ),
    # A section inside a card, as a topology node or a held request's preview
    # draws it: part of the card's frame, its head with no rule or inset.
    "in-card": Example(
        "{% card %}<p>An example line about the card.</p>"
        '{% section "Example preview" counts=lines %}{% for line in lines %}<p>{{ line }}</p>{% endfor %}'
        '{% aside %}<span class="pill tone-neutral">Example state</span>{% endsection %}{% endcard %}',
        {"lines": ("An example line in the inner section.", "Another example line.")},
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
