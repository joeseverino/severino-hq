"""The catalog's charts: lines, bars, calendars and cadences, empty and filled."""

from datetime import date, timedelta

from hq.platform.application.cadence_chart import Cadence, CadenceMatrix, CadenceRow, CadenceWeek
from hq.platform.application.ui import (
    ActivityCalendar,
    CalendarDay,
    ChartSeries,
    PlannedDay,
    PlanNote,
    WeekPlan,
    line_chart,
    stacked_bar_chart,
)
from hq.platform.core.tests.browser.catalog.common import Entry

_WEEK = date(2026, 1, 5)


def _readings(title, lede, *names, unit="min"):
    """A line chart of a week's readings, one line for each name."""

    return line_chart(
        title,
        lede,
        tuple(
            (name, tuple((_WEEK + timedelta(days=day), float((day * slot) % 5 + slot)) for day in range(7)), slot)
            for slot, name in enumerate(names, 1)
        ),
        unit=unit,
    )


def _weeks(title, lede):
    """A stacked bar chart of four weeks, two kinds in each."""

    return stacked_bar_chart(
        title,
        lede,
        ("Week 1", "Week 2", "Week 3", "Week 4"),
        (ChartSeries("Running", (3, 4, 2, 5), 1), ChartSeries("Strength", (1, 2, 2, 1), 2)),
        unit="min",
    )


def _month():
    """A month of plan against what was done, paged, with its period."""

    return ActivityCalendar(
        "Example training",
        "Planned days against what was done.",
        tuple(
            tuple(
                CalendarDay(_WEEK + timedelta(days=week * 7 + day), "done", slots=(1,), planned=True)
                if day in (0, 2)
                else CalendarDay(_WEEK + timedelta(days=week * 7 + day), "missed", planned=True)
                if day == 4
                else CalendarDay(_WEEK + timedelta(days=week * 7 + day))
                for day in range(7)
            )
            for week in range(4)
        ),
        series=(ChartSeries("Example running", (), 1),),
        period_label="January 2026",
        previous_url="/example/?month=2025-12",
        next_url="/example/?month=2026-02",
        current_url="/example/",
    )


def _matrix():
    """Three things over the same six weeks, one a link with a line about it."""

    def weeks(*marks):
        return tuple(
            CadenceWeek(label, hit=mark == "x", no_data=mark == "?", detail=f"Week of {label}")
            for label, mark in zip(("Jan 5", "Jan 12", "Jan 19", "Jan 26", "Feb 2", "Feb 9"), marks, strict=True)
        )

    return CadenceMatrix(
        ("Jan 5", "Jan 12", "Jan 19", "Jan 26", "Feb 2", "Feb 9"),
        (
            CadenceRow("Example run", weeks("x", "x", "-", "x", "x", "x"), url="/example/run/", detail="3 a week"),
            CadenceRow("Example lift", weeks("x", "-", "?", "x", "-", "x")),
            CadenceRow("An example habit with a name long enough to wrap", weeks("-", "-", "x", "x", "x", "-")),
        ),
    )


ENTRIES = {
    # Each chart partial, its head a section's: the heading, the line under
    # it, and its legend or a month's controls beside them; its data a folded
    # section in its frame.
    "chart/line": Entry(
        '{% include "partials/_line_chart.html" %}',
        {"chart": _readings("Example pace", "Example readings over a week, two of them.", "Easy", "Hard")},
    ),
    "chart/line-empty": Entry(
        '{% include "partials/_line_chart.html" %}',
        {"chart": line_chart("Example pace", "Too few readings to draw.", (), unit="example")},
    ),
    "chart/stacked-bar": Entry(
        '{% include "partials/_stacked_bar_chart.html" %}',
        {"chart": _weeks("Example sessions", "Each week's sessions by kind.")},
    ),
    "chart/activity-calendar": Entry('{% include "partials/_activity_calendar.html" %}', {"calendar": _month()}),
    "chart/tabs": Entry(
        '{% include "partials/_chart_tabs.html" with group="example" %}',
        {"charts": (_readings("Example pace", "A week of pace.", "Pace"), _weeks("Example load", "Load by week."))},
    ),
    # Two charts in a pair, one line under each heading longer than the other's,
    # and a chart beside a section of prose: each pair's frames start together.
    "chart/pair": Entry(
        '<div class="two-col">{% include "partials/_line_chart.html" with chart=first %}'
        '{% include "partials/_stacked_bar_chart.html" with chart=second %}</div>',
        {
            "first": _readings(
                "Example pace",
                "An example line under the heading long enough to take two lines beside its neighbor.",
                "Pace",
            ),
            "second": _weeks("Example sessions", "Each week's sessions."),
        },
    ),
    "chart/beside-section": Entry(
        '<div class="two-col">{% include "partials/_line_chart.html" %}'
        '{% section "Example notes" %}<p>An example line of prose in the frame.</p>{% endsection %}</div>',
        {"chart": _readings("Example pace", "Example readings over a week.", "Pace")},
    ),
    # Whether a thing happened, week by week: hits, a miss, a week counted
    # more than once, one nothing is known of, and a run up to now.
    "chart/cadence": Entry(
        '{% section "Example habits" %}{% include "partials/_cadence.html" %}{% endsection %}',
        {
            "cadence": Cadence(
                "Example stretching",
                "Each week it happened at least once.",
                (
                    CadenceWeek("Jan 5", hit=True, detail="Week of Jan 5: once"),
                    CadenceWeek("Jan 12", hit=False, detail="Week of Jan 12: not done"),
                    CadenceWeek("Jan 19", hit=False, no_data=True),
                    CadenceWeek("Jan 26", hit=True, count=3, detail="Week of Jan 26: 3 times"),
                    CadenceWeek("Feb 2", hit=True, detail="Week of Feb 2: once"),
                    CadenceWeek("Feb 9", hit=True, count=2, detail="Week of Feb 9: twice"),
                ),
            )
        },
    ),
    # Several things over the same weeks, one a link with a line about it.
    "chart/cadence-matrix": Entry(
        '{% section "Example habits" %}{% include "partials/_cadence_matrix.html" %}{% endsection %}',
        {"matrix": _matrix()},
    ),
    # The same, sharing a row with a chart.
    "chart/cadence-matrix-compact": Entry(
        '<div class="two-col">{% section "Example habits" %}'
        '{% include "partials/_cadence_matrix.html" with compact=True label="Example habit" %}{% endsection %}'
        '{% include "partials/_line_chart.html" %}</div>',
        {"matrix": _matrix(), "chart": _readings("Example minutes", "Minutes each day.", "Example run")},
    ),
    # The days of a week a thing is planned for: two on one day, a day with
    # its note, the notes under the week and the link that changes it.
    "chart/week-plan": Entry(
        '{% include "partials/_week_plan.html" %}',
        {
            "plan": WeekPlan(
                "Example week",
                (
                    PlannedDay("M", "Monday", (1,), detail="Example run"),
                    PlannedDay("T", "Tuesday"),
                    PlannedDay("W", "Wednesday", (1, 2), note="Long", detail="Example run and example lift"),
                    PlannedDay("T", "Thursday"),
                    PlannedDay("F", "Friday", (2,), detail="Example lift"),
                    PlannedDay("S", "Saturday", (1,), note="Easy", detail="Example run"),
                    PlannedDay("S", "Sunday"),
                ),
                notes=(PlanNote("Example runs", "3 a week"), PlanNote("Example lifts", "2 a week")),
                url="/example/plan/",
            )
        },
    ),
    # Shares of the largest, ranked, in a flow of sections: a whole row, a
    # sliver, and a label long enough to be cut on a phone.
    "chart/share-list": Entry(
        '<div class="section-flow">{% section "Example pages" %}{% include "partials/_share_list.html" %}'
        '{% endsection %}{% section "Example sources" %}{% include "partials/_share_list.html" with rows=few %}'
        "{% endsection %}</div>",
        {
            "rows": (
                {"label": "/", "figure": "1,204", "share": 100},
                {
                    "label": "/example/articles/an-example-article-with-a-path-long-enough-to-cut/",
                    "figure": "312",
                    "share": 25.9,
                },
                {"label": "/example/about/", "figure": "4", "share": 0.3},
            ),
            "few": ({"label": "example.org", "figure": "48", "share": 100},),
        },
    ),
}
