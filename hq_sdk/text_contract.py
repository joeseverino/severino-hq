"""How the host writes a value, pinned by example in text_examples.json.

An extension's tests see the words ``hq_sdk.ui`` and ``hq_sdk.money`` choose,
and output gets depended on: a no-break space added between a number and its
unit fails a test in another repository. So the phrasing is recorded by example
and changes the way an export's shape does, through ``manage.py sdk_contract``
and its diff.
"""

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

lazy from django.utils import timezone

lazy from hq.platform.application.derivation_testing import frozen
lazy from hq_sdk.money import money, signed_number
lazy from hq_sdk.ui import ago, counted, ended, span, when, when_day

EXAMPLES_PATH = Path(__file__).with_name("text_examples.json")

# The year every example is read in, so "this year" never moves the record.
_YEAR = 2026


def _examples() -> dict[str, list[tuple[str, Callable[[], str]]]]:
    def october(year: int, day: int, hour: int, minute: int) -> datetime:
        return datetime(year, 10, day, hour, minute, tzinfo=timezone.get_current_timezone())

    def before(**length: float) -> Callable[[], str]:
        return lambda: ago(timezone.now() - timedelta(**length))

    return {
        "span": [
            ("1", lambda: span(1)),
            ("46", lambda: span(46)),
            ("200", lambda: span(200)),
            ("-3", lambda: span(-3)),
        ],
        "when_day": [
            ("a day this year", lambda: when_day(date(_YEAR, 10, 3))),
            ("a day another year", lambda: when_day(date(_YEAR - 1, 10, 3))),
        ],
        "when": [
            ("a morning this year", lambda: when(october(_YEAR, 3, 9, 29))),
            ("an evening another year", lambda: when(october(_YEAR - 1, 3, 21, 5))),
            ("a bare date", lambda: when(date(_YEAR, 10, 3))),
        ],
        "counted": [
            ("1, project", lambda: counted(1, "project")),
            ("3, entry", lambda: counted(3, "entry")),
            (
                "1200, project needs output, projects need output",
                lambda: counted(1200, "project needs output", "projects need output"),
            ),
        ],
        "money": [
            ("1234.5", lambda: money(Decimal("1234.5"))),
            ("-5", lambda: money(Decimal(-5))),
            ("1234.4, cents=False", lambda: money(Decimal("1234.4"), cents=False)),
            ("5, plus=True", lambda: money(Decimal(5), plus=True)),
            ("None", lambda: money(None)),
        ],
        "signed_number": [
            ("-29, '{} bpm'", lambda: signed_number(-29, "{} bpm".format)),
            ("1.25, '{:.1f} min', plus=True", lambda: signed_number(1.25, "{:.1f} min".format, plus=True)),
            ("-0.04, '{:.1f} min'", lambda: signed_number(-0.04, "{:.1f} min".format)),
        ],
        "ended": [
            ("'Renews through Example'", lambda: ended("Renews through Example")),
            ("'Renews through Example, Inc.'", lambda: ended("Renews through Example, Inc.")),
        ],
        "ago": [
            ("30 seconds before", before(seconds=30)),
            ("2 hours before", before(hours=2)),
            ("5 days 3 hours before", before(days=5, hours=3)),
        ],
    }


def describe() -> dict[str, Any]:
    """Each example's output, read with the clock in ``_YEAR``."""

    with frozen(datetime(_YEAR, 6, 15, 12, tzinfo=timezone.get_current_timezone())):
        return {
            function: [{"input": label, "output": example()} for label, example in examples]
            for function, examples in _examples().items()
        }


def render(examples: dict[str, Any]) -> str:
    return json.dumps(examples, indent=2, sort_keys=True) + "\n"


def load_committed() -> dict[str, Any]:
    if not EXAMPLES_PATH.exists():
        return {}
    committed: dict[str, Any] = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))
    return committed


def drift(committed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Each example whose words changed, one line each."""

    lines: list[str] = []
    for function in sorted(set(committed) | set(current)):
        before = {example["input"]: example["output"] for example in committed.get(function, [])}
        after = {example["input"]: example["output"] for example in current.get(function, [])}
        lines.extend(
            f"~ {function}({label}): {before.get(label)!r} -> {after.get(label)!r}"
            for label in sorted(set(before) | set(after))
            if before.get(label) != after.get(label)
        )
    return lines
