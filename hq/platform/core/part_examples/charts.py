"""The chart parts' examples: a line, a month, a cadence and a week's plan."""

from datetime import date, timedelta

from hq.platform.core.template_parts import Example
lazy from hq.platform.application.cadence_chart import Cadence, CadenceMatrix, CadenceRow, CadenceWeek
lazy from hq.platform.application.ui import (
    ActivityCalendar,
    CalendarDay,
    ChartSeries,
    PlannedDay,
    PlanNote,
    WeekPlan,
    line_chart,
    stacked_bar_chart,
)

# The first day of the week every chart's readings start on.
WEEK = date(2026, 1, 5)


def readings(title, lede, *names, unit="min", marks=()):
    """A line chart of a week's readings, one line for each name."""

    return line_chart(
        title,
        lede,
        tuple(
            (name, tuple((WEEK + timedelta(days=day), float((day * slot) % 5 + slot)) for day in range(7)), slot)
            for slot, name in enumerate(names, 1)
        ),
        unit=unit,
        marks=marks,
    )


def weeks(title, lede):
    """A stacked bar chart of four weeks, two kinds in each."""

    return stacked_bar_chart(
        title,
        lede,
        ("Week 1", "Week 2", "Week 3", "Week 4"),
        (ChartSeries("Running", (3, 4, 2, 5), 1), ChartSeries("Strength", (1, 2, 2, 1), 2)),
        unit="min",
    )


def matrix():
    """Three things over the same six weeks, one a link with a line about it."""

    def marked(*marks):
        return tuple(
            CadenceWeek(label, hit=mark == "x", no_data=mark == ".", detail=f"Week of {label}")
            for label, mark in zip(("Jan 5", "Jan 12", "Jan 19", "Jan 26", "Feb 2", "Feb 9"), marks, strict=True)
        )

    return CadenceMatrix(
        ("Jan 5", "Jan 12", "Jan 19", "Jan 26", "Feb 2", "Feb 9"),
        (
            CadenceRow("Example run", marked("x", "x", "-", "x", "x", "x"), url="/example/run/", detail="3 a week"),
            CadenceRow("Example lift", marked("x", "-", ".", "x", "-", "x")),
            CadenceRow("An example habit with a name long enough to wrap", marked("-", "-", "x", "x", "x", "-")),
        ),
    )


def _month():
    """A month of plan against what was done, paged, with its period."""

    first = WEEK
    return ActivityCalendar(
        "Example training",
        "Planned days against what was done.",
        tuple(
            tuple(
                CalendarDay(first + timedelta(days=at * 7 + day), "done", slots=(1,), planned=True)
                if day in (0, 2)
                else CalendarDay(first + timedelta(days=at * 7 + day), "missed", planned=True)
                if day == 4
                else CalendarDay(first + timedelta(days=at * 7 + day))
                for day in range(7)
            )
            for at in range(4)
        ),
        series=(ChartSeries("Example running", (), 1),),
        period_label="January 2026",
        previous_url="/example/?month=2025-12",
        next_url="/example/?month=2026-02",
        current_url="/example/",
    )


# Shares of the largest, ranked: a whole row, a sliver, and a label long
# enough to be cut on a phone.
SHARES = (
    {"label": "/", "figure": "1,204", "share": 100},
    {
        "label": "/example/articles/an-example-article-with-a-path-long-enough-to-cut/",
        "figure": "312",
        "share": 25.9,
    },
    {"label": "/example/about/", "figure": "4", "share": 0.3},
)

# Each chart partial, its head a section's: the heading, the line under it,
# and its legend or a month's controls beside them; its data a folded section
# in its frame.
CHART = Example(
    context=lambda: {"chart": readings("Example pace", "Example readings over a week, two of them.", "Easy", "Hard")}
)
ACTIVITY_CALENDAR = Example(context=lambda: {"calendar": _month()})
# Whether a thing happened, week by week: hits, a miss, a week counted more
# than once, one nothing is known of, and a run up to now.
CADENCE = Example(
    context=lambda: {
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
    }
)
# Several things over the same weeks, one a link with a line about it.
CADENCE_MATRIX = Example(context=lambda: {"matrix": matrix()})
# The days of a week a thing is planned for: two on one day, a day with its
# note, the notes under the week and the link that changes it.
WEEK_PLAN = Example(
    context=lambda: {
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
    }
)
SHARE_LIST = Example(context={"rows": SHARES})
CHART_TABS = Example(
    context=lambda: {
        "charts": (readings("Example pace", "A week of pace.", "Pace"), weeks("Example load", "Load by week.")),
        "group": "example",
    }
)
