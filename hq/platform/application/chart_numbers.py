"""How a chart rounds its axis and writes its numbers."""

from __future__ import annotations

import math

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
    axis that starts at zero: its ticks are always far apart. A fitted axis is
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
