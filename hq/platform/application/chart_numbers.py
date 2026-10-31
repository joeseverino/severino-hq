"""How a chart rounds its axis and writes its numbers."""

import math

from .money import signed

# Round numbers an axis may end on. The intermediate steps keep the labels
# round while landing close to the data, so bars fill the plot rather than half
# of it.
_AXIS_STEPS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10)


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    step = next(candidate for candidate in _AXIS_STEPS if normalized <= candidate)
    return step * magnitude


def fitted_range(values: list[float], minimum: float | None, maximum: float | None) -> tuple[float, float]:
    """The low and high a line chart's axis runs between, fitted to its readings.

    A bound the caller gives is kept; one derived from the readings is padded
    so no point sits on the plot's edge.
    """
    low = minimum if minimum is not None else min(values)
    high = maximum if maximum is not None else max(values)
    if high == low:
        # A flat series still has to be drawn somewhere other than on the axis
        # itself. Half a unit either side keeps the line in the middle of the
        # plot and keeps the ticks distinct.
        return low - 0.5, high + 0.5
    padding = (high - low) * 0.08
    padded_low, padded_high = low - padding, high + padding
    # Do not invent axis below a floor the measure cannot cross. Walking
    # asymmetry is a percentage of steps and cannot be negative; padding it
    # to -0.3 draws a region no reading could ever occupy and makes the
    # data look like it is hovering above some boundary that means nothing.
    if low >= 0 > padded_low:
        padded_low = 0.0
    if high <= 100 < padded_high and low >= 0:
        padded_high = 100.0
    return padded_low, padded_high


def _format_tooltip_value(value: float) -> str:
    """The exact reading, not the axis abbreviation.

    An axis tick is glanced at and can be rounded; a tooltip is the reason
    someone pointed at the bar, so it keeps the precision the axis dropped.
    """
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


# Units written before the number, as money is. Every other unit follows it.
_CURRENCY = frozenset("$€£")


def _format_reading(value: float, unit: str) -> str:
    """One reading with its unit, as a tooltip says it: "52 bpm", "$1,250"."""
    if unit in _CURRENCY:
        # Whole units and a true minus sign, as ``money`` writes a figure that is scanned.
        return signed(f"{unit}{abs(value):,.0f}", negative=value < 0)
    return f"{_format_tooltip_value(value)} {unit}".rstrip()


def _format_table_value(value: float, unit: str) -> str:
    """One reading in the chart's data table, at the tooltip's precision.

    A column heading carries a unit that follows its numbers, so the cell is
    the number alone. Money carries its own sign in every cell.
    """
    if unit in _CURRENCY:
        return _format_reading(value, unit)
    return _format_tooltip_value(value)


def _table_heading(label: str, unit: str) -> str:
    """A data table column: the series, and its unit unless a cell or the label says it."""
    if not unit or unit in _CURRENCY or unit.casefold() == label.casefold():
        return label
    return f"{label} ({unit})"


def _format_fitted_value(value: float, span: float) -> str:
    """An axis label with enough precision for the range it sits in.

    Precision comes from the span rather than the magnitude: a fitted axis
    running from 10.6 to 11.8 min/mi under the bar chart's formatter, which
    drops decimals above ten, would read 11, 11, 12 and say nothing.
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
