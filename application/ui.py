"""Stable view models consumed by HQ's shared server-rendered UI primitives."""

from dataclasses import dataclass
import math

# The one status vocabulary. Every surface that shows state -- dashboard cards,
# insight panels, extension-provided projections -- draws from this set, so a
# state means the same thing and looks the same wherever it is rendered.
# Reserved: these never double as categorical series colours, and each is always
# rendered with its own text so state is never carried by colour alone.
STATUS_VALUES = frozenset({"good", "attention", "serious", "neutral"})


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
class ChartRow:
    label: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class StackedBarChart:
    title: str
    description: str
    unit: str
    series: tuple[ChartSeries, ...]
    bars: tuple[ChartBar, ...]
    ticks: tuple[ChartTick, ...]
    categories: tuple[ChartCategory, ...]
    rows: tuple[ChartRow, ...]
    empty: bool
    width: int = 720
    height: int = 260


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

    plot_left, plot_top, plot_width, plot_height = 48.0, 12.0, 654.0, 202.0
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
