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
) -> StackedBarChart:
    """Project raw series into one accessible, dependency-free SVG contract."""
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
    maximum = max(totals, default=0.0)
    scale_max = _nice_ceiling(maximum)
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
