"""Stable view models consumed by HQ's shared server-rendered UI primitives."""

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .workflows import WorkflowPlan

# The one status vocabulary. Every surface that shows state -- dashboard cards,
# insight panels, extension-provided projections -- draws from this set, so a
# state means the same thing and looks the same wherever it is rendered.
# Reserved: these never double as categorical series colours, and each is always
# rendered with its own text so state is never carried by colour alone.
STATUS_VALUES = frozenset({"good", "attention", "serious", "neutral"})
PAGE_SECTION_ID = re.compile(r"[a-z][a-z0-9-]*\Z")


def moment(stamp: str):
    """A provider's timestamp, parsed, or nothing when there is not one.

    Beside ``ago`` because it is never wanted without it, and shared because a
    fourth copy of "parse what a provider wrote" was about to exist. Tailscale
    writes the zero time for "never", which as an age reads as two thousand
    years and looks like a bug rather than a fact.
    """

    from datetime import datetime, timezone as _tz

    text = str(stamp or "").strip()
    if not text or text.startswith("0001-01-01"):
        return None
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=_tz.utc)


def elapsed(stamp: str) -> str:
    """A provider's timestamp as an age, or as the fact that there is none."""

    from datetime import datetime, timezone as _tz

    found = moment(stamp)
    if found is None:
        return "—"
    if found > datetime.now(_tz.utc):
        return "just now"
    return ago(found)


def ago(moment) -> str:
    """How long ago something happened, in the one phrasing HQ uses.

    Two surfaces had grown their own three-line version of this. They agreed by
    coincidence, which is the state a shared vocabulary is supposed to make
    impossible -- a page saying "4 hours ago" beside one saying "4 hours old"
    reads as two different facts.
    """

    from django.utils.timesince import timesince

    return f"{timesince(moment)} ago"


@dataclass(frozen=True)
class PageSection:
    """One stable destination in a dense page.

    Extensions declare the page's information architecture as data while HQ
    owns the navigation, responsive overflow, sticky positioning and active
    state. ``id`` is deliberately a fragment-safe public identifier: links to
    a section should survive a label being improved later.
    """

    id: str
    label: str

    def __post_init__(self) -> None:
        if not PAGE_SECTION_ID.fullmatch(self.id):
            raise ValueError(
                "PageSection id must start with a lowercase letter and contain "
                "only lowercase letters, numbers and hyphens."
            )
        if not self.label.strip():
            raise ValueError("PageSection label must not be empty.")

    @property
    def url(self) -> str:
        return f"#{self.id}"


@dataclass(frozen=True)
class PageNavigation:
    """The ordered map of a single page, rendered by HQ."""

    items: tuple[PageSection, ...]
    label: str = "On this page"

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("PageNavigation needs at least one section.")
        if not self.label.strip():
            raise ValueError("PageNavigation label must not be empty.")
        ids = tuple(item.id for item in self.items)
        if len(set(ids)) != len(ids):
            raise ValueError("PageNavigation section ids must be unique.")


@dataclass(frozen=True)
class Kpi:
    label: str
    value: str | int
    detail: str = ""
    url: str = ""
    is_zero: bool = False


@dataclass(frozen=True)
class Insight:
    """A reading, what it means, and the next action.

    The shape extensions emit for ``partials/_insight_grid.html``. Provided by
    the host so a surface needing decision support does not restate the card
    markup or re-map its own status names onto styling.
    """

    status: str
    eyebrow: str
    title: str
    value: str
    body: str
    action: str = ""
    url: str = ""
    # How many things this insight stands for, when it stands for a countable
    # backlog. ``value`` is a display string and may be a balance or a distance,
    # so a surface that totals a queue cannot infer a quantity from it. Left
    # unset, the insight counts as the single thing it describes -- which is
    # what an alert is.
    magnitude: int | None = None
    # Optional closure loop emitted by any host or plugin domain. The queue and
    # insight partials render it generically; the domain still owns every action.
    workflow: WorkflowPlan | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUS_VALUES:
            raise ValueError(
                f"Insight status must be one of {', '.join(sorted(STATUS_VALUES))}; "
                f"got {self.status!r}."
            )


@dataclass(frozen=True)
class ListRow:
    """One line of a compact record list: what it is, and when.

    The shape ``partials/_record_list.html`` renders. Surfaces that list recent
    records -- history panels, queues, "latest N with a way to see the rest" --
    emit this instead of restating the row markup, so rows align and read the
    same wherever they appear and a change to the row lands everywhere at once.

    Use ``_insight_grid.html`` instead when each entry needs interpreting; a row
    is for records that speak for themselves.
    """

    title: str
    # Inline after the title, muted: the one fact that distinguishes this row.
    detail: str = ""
    # Trailing, right-aligned: usually a date. Kept short -- it is scanned.
    meta: str = ""
    url: str = ""
    # Leaves HQ. Rendered so the operator knows before they click, and so a
    # linked page cannot reach back through window.opener.
    external: bool = False
    # Optional state. `badge` carries the text, so state is never colour alone.
    status: str = ""
    badge: str = ""

    def __post_init__(self) -> None:
        if self.status and self.status not in STATUS_VALUES:
            raise ValueError(
                f"ListRow status must be one of {', '.join(sorted(STATUS_VALUES))} "
                f"or empty; got {self.status!r}."
            )
        if self.status and not self.badge:
            raise ValueError(
                "ListRow status needs a badge: state is never carried by colour "
                "alone."
            )


@dataclass(frozen=True)
class TimelineItem:
    """One dated event in the host-owned planning horizon.

    Plugins emit meaning; HQ owns chronology, accessibility, responsive layout,
    and state styling. The date remains a real ``date`` until the template so
    callers cannot accidentally sort display strings such as "Nov 1".
    """

    when: date
    title: str
    detail: str = ""
    url: str = ""
    status: str = "neutral"
    badge: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUS_VALUES:
            raise ValueError(
                f"TimelineItem status must be one of "
                f"{', '.join(sorted(STATUS_VALUES))}; got {self.status!r}."
            )


@dataclass(frozen=True)
class Timeline:
    """A chronological, dependency-free planning surface."""

    title: str
    description: str
    items: tuple[TimelineItem, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.items, key=lambda item: (item.when, item.title)))
        if ordered != self.items:
            raise ValueError("Timeline items must be sorted chronologically.")


@dataclass(frozen=True)
class CadenceWeek:
    """One period in a "did I do this" strip.

    Answers a different question from a chart. A bar says how much; this says
    whether, week after week, at a glance -- which is what a habit is actually
    judged on. A gap in a row of filled marks is visible in a way a short bar
    beside tall ones is not.
    """

    label: str
    hit: bool
    # How many times in the period, when more than once is meaningful.
    count: int = 0
    # Read out for assistive technology, and shown on hover.
    detail: str = ""


@dataclass(frozen=True)
class Cadence:
    title: str
    description: str
    weeks: tuple[CadenceWeek, ...]

    @property
    def hits(self) -> int:
        return sum(1 for week in self.weeks if week.hit)

    @property
    def streak(self) -> int:
        """Consecutive periods, counting back from the most recent."""
        run = 0
        for week in reversed(self.weeks):
            if not week.hit:
                break
            run += 1
        return run


@dataclass(frozen=True)
class CadenceRow:
    """One thing tracked across the shared periods of a matrix."""

    label: str
    weeks: tuple[CadenceWeek, ...]
    url: str = ""
    detail: str = ""

    @property
    def hits(self) -> int:
        return sum(1 for week in self.weeks if week.hit)

    @property
    def streak(self) -> int:
        run = 0
        for week in reversed(self.weeks):
            if not week.hit:
                break
            run += 1
        return run


@dataclass(frozen=True)
class CadenceMatrix:
    """Several cadences sharing one set of periods, so they can be compared.

    Separate strips answer "did I keep this up" one at a time; stacked in
    columns that line up they answer "which of these am I neglecting", which is
    the question worth asking when there is more than one. The period labels
    appear once, at the top, because that alignment is the whole point.
    """

    periods: tuple[str, ...]
    rows: tuple[CadenceRow, ...]

    def __post_init__(self) -> None:
        for row in self.rows:
            if len(row.weeks) != len(self.periods):
                raise ValueError(
                    f"{row.label!r} has {len(row.weeks)} periods; the matrix "
                    f"has {len(self.periods)}. Columns that do not line up "
                    "make the comparison wrong rather than merely ugly."
                )


# The plot rectangle every chart in HQ draws inside. Stated once so a line and
# a bar chart placed one above the other share an axis position rather than
# nearly sharing one.
PLOT_LEFT, PLOT_TOP, PLOT_WIDTH, PLOT_HEIGHT = 48.0, 12.0, 606.0, 202.0
# The strip after the plot, and the mirror of the gutter before it. The left
# gutter holds the y-axis labels and so was always there; the right had nothing
# to hold and was given nothing, which put the last gridline, the last bar and
# the final point hard against the edge of the drawing while the opposite side
# breathed for 48 units. A chart read as leaning left, and the most recent
# period -- the one actually being looked at -- was the one with no room
# around it.
# The same number as the left rather than a smaller one chosen to taste: the
# end labels are centred on their points, so equal margins are what make the
# first and last label clear their edges by the same amount. At 24 the last
# label still hung 2px past the frame while the first cleared it by 17.
# Taken out of the plot rather than added to the box: the drawing stays 702
# units wide, so every chart keeps the aspect ratio it already had and a line
# chart sized by `height: auto` does not change height on its own.
PLOT_RIGHT = PLOT_LEFT
# A chart in a card of its own, rather than one of a pair. The drawing was
# capped at the width of a half-width card whatever it was placed in, so a
# full-width card held a 640px chart and six hundred pixels of nothing beside
# it. The height is unchanged: this is the same chart given the room it was
# put in, not a bigger one.
# Room to the right of the plot for the last category label, which is centred
# on the last point and so hangs half its width past the axis. Sized for the
# label at its largest -- a phone scales the whole drawing down, so the type
# has to be enlarged in these units to survive it, and the overhang is twice
# what a desktop needs. Padding rather than a special case for the final
# label: re-anchoring one label moves it off the point it belongs to.
# The drawing is only the drawing now. Labels are HTML positioned over it, so
# the box needs no room for type that is no longer inside it -- and one
# geometry serves a half-width card and a full-width one alike, because
# stretching rectangles is not the same as stretching words.
STANDARD_SVG = round(PLOT_LEFT + PLOT_WIDTH + PLOT_RIGHT)
STANDARD_HEIGHT = 260
# What an axis label costs, in pixels, at the 11px it is rendered at: roughly
# six per character, plus a gap before the next one. Used to work out whether a
# set of labels can fit a narrow card at all -- a count cannot answer that,
# because "Jan 2026" needs half again what "Jul 6" does, and it was a count
# that let eight month names through to overlap each other seven times.
LABEL_CHAR_PX = 6.0
LABEL_GAP_PX = 8.0
# The narrowest card an axis is expected to fill, less the gutter its y-axis
# labels sit in. The container query that acts on `dense` uses the same figure.
NARROW_PLOT_PX = 420.0


@dataclass(frozen=True)
class ChartSeries:
    label: str
    values: tuple[float, ...]
    slot: int


@dataclass(frozen=True)
class ChartBar:
    x: float
    y: float
    width: float
    height: float
    value: float
    label: str
    slot: int
    # Rendered by the host's own tooltip rather than an SVG <title>. The native
    # one waits a second or two before appearing, which is long enough that
    # reading a chart stops feeling like reading and starts feeling like
    # querying.
    tooltip: str = ""


@dataclass(frozen=True)
class ChartTick:
    y: float
    label: str


@dataclass(frozen=True)
class ChartCategory:
    x: float
    label: str


@dataclass(frozen=True)
class PlacedLabel:
    """An axis label and where it sits, as a share of the drawing.

    Rendered outside the SVG, so it needs its position as a percentage rather
    than in chart units. Derived by the chart rather than supplied with the
    category, because the share depends on the chart's width and a caller
    building its own categories -- the mile profile and the route elevation
    both do -- has no reason to know it. Asked for it, all three forgot, and
    every label on those charts stacked at zero.
    """

    at: float
    label: str


@dataclass(frozen=True)
class ChartRow:
    label: str
    values: tuple[float, ...]


@dataclass(frozen=True, kw_only=True)
class Chart:
    """What every chart in HQ is, and the one place its axis is derived.

    A bar chart and a line chart differ in what they draw -- rectangles or a
    path -- and in nothing else. They share a plot rectangle, a set of ticks, a
    set of categories, the table underneath, and the four derivations that turn
    those into something a template can position. Held separately, they agreed
    by inspection: the same `dense`, `gutter`, `axis_x` and `axis_y` were
    written out twice, and the copies stopped agreeing exactly where you would
    expect. `plot_right` was added to the bar chart to stop its template
    hardcoding 702; the line chart's template went on hardcoding 702, 48, 12
    and 214, because there was no shared thing for the fix to land on.

    So the shared part is one class and the difference is the subclass. A third
    chart type inherits a correct axis instead of copying one, which is the
    only version of this that stays true as the drawing types multiply.

    Keyword-only: every field has a default below the ones that do not, and
    subclasses add their own required fields after them. Positional
    construction would make that an ordering puzzle; nothing constructs these
    positionally, so the puzzle is simply removed.
    """

    title: str
    description: str
    unit: str
    ticks: tuple[ChartTick, ...]
    categories: tuple[ChartCategory, ...]
    rows: tuple[ChartRow, ...]
    empty: bool
    width: int = STANDARD_SVG
    height: int = STANDARD_HEIGHT
    # The plot's own edges. Carried rather than hardcoded in the template,
    # which drew gridlines to 702 whatever the chart's own width was. All four
    # are here because the line chart went on hardcoding the other three after
    # `plot_right` was introduced for the bar chart, and a constant copied into
    # a template is a constant that stops agreeing the moment this file moves.
    plot_left: float = PLOT_LEFT
    plot_right: float = PLOT_LEFT + PLOT_WIDTH
    plot_top: float = PLOT_TOP
    plot_bottom: float = PLOT_TOP + PLOT_HEIGHT

    @property
    def dense(self) -> bool:
        """Whether these labels can fit a narrow card side by side.

        Measured, not counted: the width they need is the sum of what each one
        costs, and that is knowable here because the text is here. How much
        room they actually get is knowable only to the browser, so this says
        its half -- these labels want more than a narrow card has -- and a
        container query says the other half, whether the card they landed in
        is that narrow.
        """
        if not self.categories:
            return False
        text = sum(len(item.label) for item in self.categories) * LABEL_CHAR_PX
        gaps = (len(self.categories) - 1) * LABEL_GAP_PX
        return text + gaps > NARROW_PLOT_PX

    @property
    def gutter(self) -> float:
        """Where the plot starts, as a share of the drawing.

        The y-axis labels sit in the strip to the left of it. Given a fixed
        width instead, that strip stayed 44px while the drawing's own gutter
        shrank with the card, and on a phone the first date label was printed
        on top of the bottom tick.

        Read from `plot_left` rather than the module constant, so a chart that
        is given a plot different from the standard one reports the gutter it
        actually has rather than the one this file happens to default to.
        """
        return self.plot_left / self.width * 100

    @property
    def axis_x(self) -> tuple:
        return tuple(
            PlacedLabel(item.x / self.width * 100, item.label)
            for item in self.categories
        )

    @property
    def axis_y(self) -> tuple:
        return tuple(
            PlacedLabel(item.y / self.height * 100, item.label)
            for item in self.ticks
        )


@dataclass(frozen=True, kw_only=True)
class StackedBarChart(Chart):
    """Quantities that add up, on a zero-based axis."""

    series: tuple[ChartSeries, ...]
    bars: tuple[ChartBar, ...]




@dataclass(frozen=True)
class LinePoint:
    """One reading, placed."""

    x: float
    y: float
    value: float
    label: str
    tooltip: str = ""


@dataclass(frozen=True)
class LineSeries:
    """One line, already projected into the plot's coordinates."""

    label: str
    slot: int
    path: str
    points: tuple[LinePoint, ...]
    # A fitted line through the same points, drawn dashed. Optional because a
    # trend is a claim: it belongs on a series where the direction is the
    # question, and nowhere else.
    trend: str = ""


@dataclass(frozen=True)
class LineMark:
    """A vertical rule at a date -- the day something began."""

    x: float
    label: str


@dataclass(frozen=True, kw_only=True)
class LineChart(Chart):
    """A measure over time, on an axis fitted to the measure.

    Not a bar chart with the bars removed. `stacked_bar_chart` is zero-based by
    contract, which is right for quantities that add up -- minutes trained,
    volume lifted -- and wrong for anything that varies around a level. Every
    mile of a run sits between 140 and 160 bpm, and drawn from zero those are
    identical bars: the chart says nothing changed, which is the opposite of
    what the numbers say. This axis is fitted to the data's own range, so the
    variation is the drawing.

    The trade is real and worth stating: a fitted axis exaggerates small
    movements, which is exactly why the range is printed on the axis and the
    numbers stay in the table underneath.

    Everything about where its axis sits comes from `Chart`. What is declared
    here is only what a line has and a bar does not.
    """

    series: tuple[LineSeries, ...]
    marks: tuple[LineMark, ...]




# How much room a date label needs beside its neighbour. "Aug 14" is about
# forty pixels at the axis size, and two of them closer than this print over
# one another rather than beside each other.
LABEL_GAP = 52.0


def line_chart(
    title: str,
    description: str,
    series: "tuple[tuple[str, tuple[tuple[date, float], ...], int], ...]",
    *,
    unit: str,
    marks: "tuple[tuple[date, str], ...]" = (),
    trend: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> LineChart:
    """Dated readings as lines, on an axis fitted to what they actually are.

    Each series is `(label, ((date, value), ...), slot)`. Dates rather than
    positions: readings are not evenly spaced, and plotting against index
    silently restates a month's gap as one step.
    """
    cleaned = [
        (label, tuple(sorted(points)), slot)
        for label, points, slot in series
        if points
    ]
    if any(item[2] not in range(1, 6) for item in cleaned):
        raise ValueError("Chart series slots must be between 1 and 5.")
    slots = [item[2] for item in cleaned]
    if len(set(slots)) != len(slots):
        # The slot is the colour. Two series sharing one draws a legend with
        # the same swatch twice and two indistinguishable lines under it --
        # a chart that looks finished and cannot be read. Caught here because
        # it is invisible in the data and only shows up on the rendered page.
        raise ValueError(
            f"Chart series must not share a colour slot: {sorted(slots)}."
        )
    every = [value for _, points, _ in cleaned for _, value in points]
    if not every or len(every) < 2:
        return LineChart(
            title=title,
            description=description,
            unit=unit,
            series=(),
            ticks=(),
            categories=(),
            marks=(),
            rows=(),
            empty=True,
        )

    days = [day for _, points, _ in cleaned for day, _ in points]
    first_day, last_day = min(days), max(days)
    span = max(1, (last_day - first_day).days)

    low = minimum if minimum is not None else min(every)
    high = maximum if maximum is not None else max(every)
    if high == low:
        # A flat series still has to be drawn somewhere other than on the axis
        # itself. Half a unit either side keeps the line in the middle of the
        # plot and keeps the ticks distinct.
        low, high = low - 0.5, high + 0.5
    else:
        padding = (high - low) * 0.08
        floor, ceiling = low, high
        low, high = low - padding, high + padding
        # Do not invent axis below a floor the measure cannot cross. Walking
        # asymmetry is a percentage of steps and cannot be negative; padding it
        # to -0.3 draws a region no reading could ever occupy and makes the
        # data look like it is hovering above some boundary that means nothing.
        if floor >= 0 > low:
            low = 0.0
        if ceiling <= 100 and high > 100 and floor >= 0:
            high = 100.0

    def place_x(day) -> float:
        return PLOT_LEFT + PLOT_WIDTH * ((day - first_day).days / span)

    def place_y(value: float) -> float:
        return PLOT_TOP + PLOT_HEIGHT * (1 - (value - low) / (high - low))

    lines = []
    for label, points, slot in cleaned:
        placed = tuple(
            LinePoint(
                x=place_x(day),
                y=place_y(value),
                value=value,
                label=f"{day:%b %-d, %Y}",
                tooltip=f"{day:%b %-d, %Y} · {label}: {_format_tooltip_value(value)} {unit}",
            )
            for day, value in points
        )
        path = " ".join(
            f"{'M' if index == 0 else 'L'} {p.x:.1f} {p.y:.1f}"
            for index, p in enumerate(placed)
        )
        lines.append(
            LineSeries(
                label=label,
                slot=slot,
                path=path,
                points=placed,
                trend=_trend_path(points, place_x, place_y) if trend else "",
            )
        )

    ticks = tuple(
        ChartTick(place_y(value), _format_fitted_value(value, high - low))
        for value in (low, (low + high) / 2, high)
    )
    # Six labels at most: a date every few pixels is a smear, not an axis.
    step = max(1, (span + 1) // 5)
    stamps = []
    cursor = first_day
    while cursor <= last_day:
        stamps.append(cursor)
        cursor += timedelta(days=step)
    # The last day is always labelled -- it is the one an eye goes to -- but
    # stepping from the first rarely lands on it, so the step before it can
    # fall a couple of days short and the two labels print on top of each
    # other. Whichever of the pair is not the last day gives way.
    if stamps[-1] != last_day:
        while len(stamps) > 1 and place_x(last_day) - place_x(stamps[-1]) < (
            LABEL_GAP
        ):
            stamps.pop()
        stamps.append(last_day)
    # Day and month where the chart covers less than a year, month and year
    # where it covers more. "Sep 15" across six years of history reads as a
    # single year, which hides the thing the chart is for.
    span_days = (last_day - first_day).days
    stamp = "%b %Y" if span_days > 400 else "%b %-d"
    categories = tuple(
        ChartCategory(place_x(day), f"{day:{stamp}}") for day in stamps
    )

    dated: dict = {}
    for label, points, _ in cleaned:
        for day, value in points:
            dated.setdefault(day, {})[label] = value
    rows = tuple(
        ChartRow(
            f"{day:%b %-d, %Y}",
            tuple(dated[day].get(label, 0.0) for label, _, _ in cleaned),
        )
        for day in sorted(dated)
    )

    return LineChart(
        title=title,
        description=description,
        unit=unit,
        series=tuple(lines),
        ticks=ticks,
        categories=categories,
        marks=tuple(
            LineMark(place_x(day), label)
            for day, label in marks
            if first_day <= day <= last_day
        ),
        rows=rows,
        empty=False,
    )


def _trend_path(points, place_x, place_y) -> str:
    """A least-squares line across the plot, or nothing when it cannot be fit."""
    if len(points) < 3:
        return ""
    xs = [float(day.toordinal()) for day, _ in points]
    ys = [float(value) for _, value in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    variation = sum((x - x_mean) ** 2 for x in xs)
    if variation == 0:
        return ""
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / variation
    intercept = y_mean - slope * x_mean
    first, last = points[0][0], points[-1][0]
    return (
        f"M {place_x(first):.1f} {place_y(intercept + slope * first.toordinal()):.1f} "
        f"L {place_x(last):.1f} {place_y(intercept + slope * last.toordinal()):.1f}"
    )


# What a calendar day can be. "planned" is a commitment not yet due; "missed"
# is one whose day has passed. They are separate states because they call for
# opposite reactions, and a single hollow ring for both makes the plan look
# broken every time you check it mid-week.
CALENDAR_STATES = frozenset({"done", "missed", "planned", "empty"})


@dataclass(frozen=True)
class CalendarDay:
    """One day: what the plan asked for, and what actually happened.

    `slots` are ChartSeries slot numbers, so a filled dot takes the same colour
    as that series' band in the chart beside it. The shared vocabulary is the
    point -- "green is strength" has to mean the same thing in both, or reading
    them together is worse than reading either alone.
    """

    when: date
    state: str = "empty"
    slots: tuple[int, ...] = ()
    detail: str = ""
    # Whether the plan asked for this day, kept separately from `state` so a
    # completed day can still say whether it was scheduled or extra.
    planned: bool = False
    # A day outside the window, rendered as a spacer so the weekday columns
    # line up. Alignment is what makes the grid readable as weeks at all.
    filler: bool = False

    def __post_init__(self) -> None:
        if self.state not in CALENDAR_STATES:
            raise ValueError(
                f"CalendarDay state must be one of "
                f"{', '.join(sorted(CALENDAR_STATES))}; got {self.state!r}."
            )
        if self.state in ("missed", "planned") and not self.planned:
            raise ValueError(
                f"A {self.state!r} day is by definition planned; set planned=True."
            )


@dataclass(frozen=True)
class ActivityCalendar:
    """Plan against execution, day by day, laid out as weeks.

    A chart answers how much and a cadence strip answers whether, per week.
    This answers *when* -- which days the plan asks for, which were kept, and
    which were not. None of that survives aggregation into a weekly bar: four
    sessions crammed into a weekend and four spread across the week produce
    the same bar and are not the same training.
    """

    title: str
    description: str
    weeks: tuple[tuple[CalendarDay, ...], ...]
    series: tuple[ChartSeries, ...] = ()
    # The period being shown, e.g. "May 2026". A calendar without one asks the
    # reader to infer the month from the date numbers, which they cannot do
    # when the grid spans two.
    period_label: str = ""
    # Paging. Blank means this surface does not offer it -- a composed overview
    # shows the current period and links to the domain for the rest.
    previous_url: str = ""
    next_url: str = ""
    # Jump back to the period the data ends in. Blank when already there.
    current_url: str = ""

    def __post_init__(self) -> None:
        for week in self.weeks:
            if len(week) != 7:
                raise ValueError(
                    f"A calendar week needs 7 days for the columns to line up; "
                    f"got {len(week)}. Pad the ends with filler days."
                )

    def _count(self, state: str) -> int:
        return sum(1 for week in self.weeks for day in week if day.state == state)

    @property
    def done(self) -> int:
        return self._count("done")

    @property
    def missed(self) -> int:
        return self._count("missed")

    @property
    def kept(self) -> str:
        """Planned days kept, as "n/m" -- blank when nothing has come due.

        Counts only days the plan asked for and whose day has passed. Unplanned
        training is real work and shows as a filled dot, but crediting it here
        would let extra sessions paper over a schedule that is not being kept;
        counting days still ahead would make every Monday look like a failure.
        """
        kept = sum(
            1
            for week in self.weeks
            for day in week
            if day.planned and day.state == "done"
        )
        due = kept + self.missed
        return f"{kept}/{due}" if due else ""


@dataclass(frozen=True)
class PlannedDay:
    """One weekday in a recurring plan.

    Several things can be scheduled on the same day -- a run and a lift -- so
    marks are a tuple of ChartSeries slots rather than one flag. `note` names
    what the day is for when the dot alone does not say it.
    """

    label: str
    full_label: str
    slots: tuple[int, ...] = ()
    note: str = ""
    detail: str = ""

    @property
    def planned(self) -> bool:
        return bool(self.slots)


@dataclass(frozen=True)
class PlanNote:
    label: str
    value: str


@dataclass(frozen=True)
class WeekPlan:
    """The recurring shape of a week: what is scheduled, on which days.

    Distinct from an ActivityCalendar, which records what happened on real
    dates. This is the commitment those dates get judged against, and it is
    worth stating on its own -- a plan that lives only in the operator's head
    cannot be compared to anything.
    """

    title: str
    days: tuple[PlannedDay, ...]
    notes: tuple[PlanNote, ...] = ()
    url: str = ""

    def __post_init__(self) -> None:
        if len(self.days) != 7:
            raise ValueError(f"A week plan needs 7 days; got {len(self.days)}.")


@dataclass(frozen=True)
class DomainOverview:
    """One plugin's useful contribution to a cross-domain surface.

    The domain owns readings and interpretation. HQ owns rendering primitives,
    so adding a domain requires no import or template change in the composer.

    Deliberately carries no insights. A domain says "this needs a decision"
    through `attention_provider` and nowhere else, because that channel is
    complete and severity-ordered by the host. An overview is a display
    surface, and display surfaces truncate -- the first version of this field
    was populated with `insights[:3]` from a list built in derivation order, so
    a domain's fourth insight could be `serious` and still never reach the
    composed queue. Two channels for one question means the quieter one wins
    silently. There is one channel.
    """

    description: str
    url: str
    kpis: tuple[Kpi, ...]
    charts: tuple[StackedBarChart, ...] = ()
    calendars: tuple[ActivityCalendar, ...] = ()
    timeline: tuple[TimelineItem, ...] = ()


def stacked_bar_chart(
    title: str,
    description: str,
    labels: tuple[str, ...],
    series: tuple[ChartSeries, ...],
    *,
    unit: str,
    maximum: float | None = None,
) -> StackedBarChart:
    """Project raw series into one accessible, dependency-free SVG contract.

    ``maximum`` fixes the axis top instead of deriving a round ceiling from the
    data, for scales that are known rather than observed.
    """
    if any(item.slot not in range(1, 6) for item in series):
        raise ValueError("Chart series slots must be between 1 and 5.")
    if any(len(item.values) != len(labels) for item in series):
        raise ValueError("Every chart series must align with the category labels.")
    if any(
        value < 0 or not math.isfinite(value)
        for item in series
        for value in item.values
    ):
        raise ValueError("Chart values must be finite and non-negative.")

    plot_left, plot_top, plot_width, plot_height = (
        PLOT_LEFT,
        PLOT_TOP,
        PLOT_WIDTH,
        PLOT_HEIGHT,
    )
    svg_width = STANDARD_SVG
    totals = tuple(
        sum(item.values[index] for item in series) for index in range(len(labels))
    )
    # A caller that knows the scale says so. Shares of a whole always run to
    # 100, and letting the axis round up to 150 leaves a third of the plot
    # permanently empty and makes a full bar look like a partial one.
    scale_max = maximum or _nice_ceiling(max(totals, default=0.0))
    column = plot_width / max(len(labels), 1)
    bar_width = min(34.0, column * 0.62)
    bars: list[ChartBar] = []
    categories: list[ChartCategory] = []
    for index, label in enumerate(labels):
        x = plot_left + index * column + (column - bar_width) / 2
        categories.append(ChartCategory(x + bar_width / 2, label))
        stacked = 0.0
        for item in series:
            value = float(item.values[index])
            height = (value / scale_max) * plot_height if scale_max else 0.0
            stacked += height
            bars.append(
                ChartBar(
                    x=x,
                    y=plot_top + plot_height - stacked,
                    width=bar_width,
                    height=height,
                    value=value,
                    label=item.label,
                    slot=item.slot,
                    tooltip=(
                        f"{label} · {item.label}: "
                        f"{_format_tooltip_value(value)} {unit}"
                    ),
                )
            )
    ticks = tuple(
        ChartTick(
            plot_top + plot_height - (value / scale_max) * plot_height,
            _format_chart_value(value),
        )
        for value in (0.0, scale_max / 2, scale_max)
    )
    rows = tuple(
        ChartRow(label, tuple(float(item.values[index]) for item in series))
        for index, label in enumerate(labels)
    )
    return StackedBarChart(
        title=title,
        description=description,
        unit=unit,
        series=series,
        bars=tuple(bars),
        ticks=ticks,
        categories=tuple(categories),
        rows=rows,
        empty=not any(totals),
        width=svg_width,
        plot_right=plot_left + plot_width,
    )


# Round numbers an axis may end on. The coarse (1, 2, 5, 10) set forced a
# maximum of 57k up to 100k, leaving bars filling barely half the plot height --
# the chart read as mostly empty space. These intermediate steps keep the labels
# round while landing much closer to the data.
_AXIS_STEPS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10)


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    step = next(candidate for candidate in _AXIS_STEPS if normalized <= candidate)
    return step * magnitude


def _format_tooltip_value(value: float) -> str:
    """The exact reading, not the axis abbreviation.

    An axis tick is glanced at and can be rounded; a tooltip is the reason
    someone pointed at the bar, so it keeps the precision the axis dropped.
    """
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def _format_fitted_value(value: float, span: float) -> str:
    """An axis label with enough precision for the range it sits in.

    The bar chart's formatter drops decimals above ten, which is right for an
    axis that starts at zero -- its ticks are always far apart. A fitted axis is
    not: a pace chart running from 10.6 to 11.8 min/mi has three ticks that all
    round to "11", and an axis reading 11, 11, 12 looks broken and says nothing.

    Precision therefore comes from the span rather than the magnitude.
    """
    if span >= 10 or not span:
        return _format_chart_value(value)
    decimals = 1 if span >= 1 else 2
    return f"{value:,.{decimals}f}"


def _format_chart_value(value: float) -> str:
    """Axis labels, compacted.

    Axis ticks are glanced at, not read digit by digit, so large magnitudes are
    abbreviated: an unabbreviated "100000" is wide enough to crowd the plot and
    slower to parse than "100k". Exact values stay available in the chart's data
    table, which the primitive always renders.
    """
    magnitude = abs(value)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if magnitude >= threshold:
            scaled = value / threshold
            # One decimal only when it adds information (1.5k, but 100k not 100.0k).
            text = f"{scaled:.0f}" if scaled >= 10 or scaled.is_integer() else f"{scaled:.1f}"
            return f"{text}{suffix}"
    return f"{value:.0f}" if value >= 10 or value.is_integer() else f"{value:.1f}"
