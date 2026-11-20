"""The layout's examples: each kind, holding the runs of sections a page gives it."""

from hq.platform.core.part_examples import NARROW
from hq.platform.core.part_examples.charts import readings, weeks
from hq.platform.core.template_parts import Example


def _sections(*sections):
    """Sections each named and holding what it is given."""

    return "".join(f'{{% section "{heading}" %}}{body}{{% endsection %}}' for heading, body in sections)


def _flow(*sections):
    return "{% layout %}" + _sections(*sections) + "{% endlayout %}"


# A wide section's body: a table of four columns, which takes the whole row.
_WIDE = (
    '{% table_region "Example hosts" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Name" %}{% table_heading "State" drop=3 %}{% table_heading "Address" drop=1 %}'
    '{% table_heading "Seen" drop=2 %}'
    "</tr></thead><tbody><tr><td>example-01</td><td>Online</td><td>192.0.2.10</td><td>Today</td></tr></tbody>"
    "</table>{% endtable_region %}"
)

# A long section's body: more facts than a narrow one holds.
_LONG = (
    "{% facts %}"
    + "".join(f'{{% fact "Example fact {n}" value="example-{n:02}" %}}' for n in range(1, 7))
    + "{% endfacts %}"
)


LAYOUT_STATES = {
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
        _flow(("Example identity 5", NARROW + "{% foot %}Example of how to read it."), ("Example link 5", _LONG))
    ),
    # A domain's charts, a row of their own, each at the width it keeps its scale.
    "charts": Example(
        '{% layout kind="charts" %}{% include "partials/_chart.html" with chart=pace %}'
        '{% include "partials/_chart.html" with chart=weeks %}{% endlayout %}',
        lambda: {
            "pace": readings("Example pace", "Example readings over a week.", "Easy", "Hard"),
            "weeks": weeks("Example weeks", "Example minutes each week."),
        },
    ),
    # A pair, like beside like: a table beside a list of facts as long.
    "pair": Example(
        '{% layout kind="pair" %}' + _sections(("Example hosts 2", _WIDE), ("Example facts", _LONG)) + "{% endlayout %}"
    ),
    # A main with its companion, a flow of sections drawn as its aside: the
    # companion sized to what it holds, the two sections pairing once they
    # wrap under the main.
    "split": Example(
        '{% layout kind="split" id="example-split" data_example="example" %}'
        + _sections(("Example records", _LONG))
        + "{% layout aside=True %}"
        + _sections(("Example identity 6", NARROW), ("Example link 6", NARROW))
        + "{% endlayout %}{% endlayout %}"
    ),
}
