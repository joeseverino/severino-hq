"""The catalog's charts: lines, bars, calendars and cadences, empty and filled."""

from datetime import timedelta

from hq.platform.application.path_chart import PathChart, PathMarker, PathScale, PathSegment
from hq.platform.application.ui import (
    line_chart,
)
from hq.platform.core.part_examples.charts import SHARES, WEEK, matrix, readings, weeks
from hq.platform.core.template_parts import Example

ENTRIES = {
    "chart/path": Example(
        '{% include "partials/_chart.html" %}',
        {
            "chart": PathChart(
                title="Example path",
                description="A path colored by its reading.",
                segments=(
                    PathSegment("M 20 40 L 150 120 L 280 30", 1, "First reading"),
                    PathSegment("M 280 30 L 360 160 L 20 40", 5, "Last reading"),
                ),
                markers=(PathMarker(20, 40, "Start"), PathMarker(150, 120, "Finish")),
                scale=PathScale(20, 180, 80, "100 m"),
                bands=((1, "Lower"), (5, "Higher")),
                width=400,
                height=220,
                note="An example reading.",
            )
        },
    ),
    "chart/path-empty": Example(
        '{% include "partials/_chart.html" %}',
        {"chart": PathChart(title="Example path", description="A path.", segments=(), width=400, height=220)},
    ),
    # A rule at a day something began, its label over the rule.
    "chart/line-marked": Example(
        '{% include "partials/_chart.html" %}',
        {"chart": readings("Example pace", "", "Easy", marks=((WEEK + timedelta(days=3), "Example start"),))},
    ),
    # Two years of readings, so each label carries its year and the last one,
    # the longest an axis draws, ends at the plot's right edge.
    "chart/line-years": Example(
        '{% include "partials/_chart.html" %}',
        {
            "chart": line_chart(
                "Example weight",
                "",
                (
                    (
                        "Weight",
                        tuple((WEEK + timedelta(days=36 * step), float(70 + step % 4)) for step in range(21)),
                        1,
                    ),
                ),
                unit="kg",
            )
        },
    ),
    "chart/line-empty": Example(
        '{% include "partials/_chart.html" %}',
        {"chart": line_chart("Example pace", "Too few readings to draw.", (), unit="example")},
    ),
    "chart/stacked-bar": Example(
        '{% include "partials/_chart.html" %}',
        {"chart": weeks("Example sessions", "Each week's sessions by kind.")},
    ),
    "chart/tabs": Example(
        '{% include "partials/_chart_tabs.html" with group="example" %}',
        {"charts": (readings("Example pace", "A week of pace.", "Pace"), weeks("Example load", "Load by week."))},
    ),
    # Two charts in a pair, one line under each heading longer than the other's,
    # and a chart beside a section of prose: each pair's frames start together.
    "chart/pair": Example(
        '<div class="two-col">{% include "partials/_chart.html" with chart=first %}'
        '{% include "partials/_chart.html" with chart=second %}</div>',
        {
            "first": readings(
                "Example pace",
                "An example line under the heading long enough to take two lines beside its neighbor.",
                "Pace",
            ),
            "second": weeks("Example sessions", "Each week's sessions."),
        },
    ),
    "chart/beside-section": Example(
        '<div class="two-col">{% include "partials/_chart.html" %}'
        '{% section "Example notes" %}<p>An example line of prose in the frame.</p>{% endsection %}</div>',
        {"chart": readings("Example pace", "Example readings over a week.", "Pace")},
    ),
    # The same, sharing a row with a chart.
    "chart/cadence-matrix-compact": Example(
        '<div class="two-col">{% section "Example habits 3" %}'
        '{% include "partials/_cadence_matrix.html" with compact=True label="Example habit" %}{% endsection %}'
        '{% include "partials/_chart.html" %}</div>',
        {"matrix": matrix(), "chart": readings("Example minutes", "Minutes each day.", "Example run")},
    ),
    # Shares of the largest, ranked, in a flow of sections: a whole row, a
    # sliver, and a label long enough to be cut on a phone.
    "chart/share-list": Example(
        '{% layout %}{% section "Example pages" %}{% include "partials/_share_list.html" %}'
        '{% endsection %}{% section "Example sources" %}{% include "partials/_share_list.html" with rows=few %}'
        "{% endsection %}{% endlayout %}",
        {
            "rows": SHARES,
            "few": ({"label": "example.org", "figure": "48", "share": 100},),
        },
    ),
}
