"""How the host writes a value, by example.

An extension's tests see the words ``hq_sdk.ui`` and ``hq_sdk.money`` choose,
and output gets depended on: a no-break space added between a number and its
unit fails a test in another repository. So each phrasing is written out here,
and a change of wording shows in this file's diff.
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone

from hq.platform.application.derivation_testing import frozen
from hq_sdk.money import money, signed_number
from hq_sdk.ui import ago, counted, ended, measured, span, when, when_day

# The year every example is read in, so "this year" never moves an answer.
_YEAR = 2026


def _october(year: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, 10, day, hour, minute, tzinfo=timezone.get_current_timezone())


def _before(**length: float) -> Callable[[], str]:
    return lambda: ago(timezone.now() - timedelta(**length))


class HostWordsTests(SimpleTestCase):
    def assertWords(self, examples: list[tuple[Callable[[], str], str]]) -> None:
        with frozen(datetime(_YEAR, 6, 15, 12, tzinfo=timezone.get_current_timezone())):
            for index, (example, expected) in enumerate(examples):
                with self.subTest(example=index):
                    self.assertEqual(example(), expected)

    def test_span(self):
        self.assertWords(
            [
                (lambda: span(1), "1 day"),
                (lambda: span(46), "46 days"),
                (lambda: span(200), "6\xa0months, 2\xa0weeks"),
                (lambda: span(-3), "3 days overdue"),
            ]
        )

    def test_when_day(self):
        self.assertWords(
            [
                (lambda: when_day(date(_YEAR, 10, 3)), "Oct\xa03"),
                (lambda: when_day(date(_YEAR - 1, 10, 3)), "Oct\xa03,\xa02025"),
            ]
        )

    def test_when(self):
        self.assertWords(
            [
                (lambda: when(_october(_YEAR, 3, 9, 29)), "Oct\xa03, 9:29\xa0AM"),
                (lambda: when(_october(_YEAR - 1, 3, 21, 5)), "Oct\xa03,\xa02025, 9:05\xa0PM"),
                (lambda: when(date(_YEAR, 10, 3)), "Oct\xa03"),
            ]
        )

    def test_counted(self):
        self.assertWords(
            [
                (lambda: counted(1, "project"), "1 project"),
                (lambda: counted(3, "entry"), "3 entries"),
                (
                    lambda: counted(1200, "project needs output", "projects need output"),
                    "1,200 projects need output",
                ),
            ]
        )

    def test_money(self):
        self.assertWords(
            [
                (lambda: money(Decimal("1234.5")), "$1,234.50"),
                (lambda: money(Decimal(-5)), "\u2212\u2060$5.00"),
                (lambda: money(Decimal("1234.4"), cents=False), "$1,234"),
                (lambda: money(Decimal(5), plus=True), "+\u2060$5.00"),
                (lambda: money(None), "\u2013"),
            ]
        )

    def test_signed_number(self):
        self.assertWords(
            [
                (lambda: signed_number(-29, "{} bpm".format), "\u2212\u206029 bpm"),
                (lambda: signed_number(1.25, "{:.1f} min".format, plus=True), "+\u20601.2 min"),
                (lambda: signed_number(-0.04, "{:.1f} min".format), "0.0 min"),
            ]
        )

    def test_measured(self):
        self.assertWords(
            [
                (lambda: measured(4.256, "mi", 2), "4.26 mi"),
                (lambda: measured(1234.5, "ft", 0), "1,235 ft"),
                (lambda: measured(12.5, "%", -1), "12.5%"),
                (lambda: measured(-0.04, "min", 1), "0.0 min"),
                (lambda: measured(0, "more"), "0 more"),
                (lambda: measured(None, "mi"), "\u2013"),
            ]
        )

    def test_ended(self):
        self.assertWords(
            [
                (lambda: ended("Renews through Example"), "Renews through Example."),
                (lambda: ended("Renews through Example, Inc."), "Renews through Example, Inc."),
            ]
        )

    def test_ago(self):
        self.assertWords(
            [
                (_before(seconds=30), "just now"),
                (_before(hours=2), "2\xa0hours ago"),
                (_before(days=5, hours=3), "5\xa0days ago"),
            ]
        )
