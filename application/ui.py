"""Stable view models consumed by HQ's shared server-rendered UI primitives."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Kpi:
    label: str
    value: str | int
    detail: str = ""
    url: str = ""
    is_zero: bool = False


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


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    step = next(candidate for candidate in (1, 2, 5, 10) if normalized <= candidate)
    return step * magnitude


def _format_chart_value(value: float) -> str:
    return f"{value:.0f}" if value >= 10 or value.is_integer() else f"{value:.1f}"
