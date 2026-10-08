"""An extension's part of a shared composition, derived once per change by the host."""

from datetime import date
from functools import partial
from unittest import mock

from django.test import SimpleTestCase, TestCase

from hq.platform.application import derivations
from hq.platform.application.demo import demo_scope, showing_demo
from hq.platform.application.derivations import DERIVATIONS, counting
from hq.platform.application.plugins import (
    _identity,
    answered_by,
    provided,
)
from hq.platform.application.projection import projection_scope
from hq.platform.application.ui import Insight
from hq.platform.core.models import UpstreamReading

from .test_derivations import MOMENT

WINDOW = (date(2026, 1, 1), date(2026, 12, 31))
ITEM = Insight("serious", "Alpha", "Something is wrong", "1", "Body.")
CARDS = (
    {"id": "alpha-open", "label": "Open", "value": 3, "url": "/alpha/"},
    {"id": "alpha-late", "label": "Late", "value": 1, "url": "/alpha/"},
)


def _readings() -> int:
    return UpstreamReading.objects.count()


def _write(key: str = "one") -> None:
    UpstreamReading.objects.create(key=key, value={}, observed_at=MOMENT)


def _counting_items() -> tuple[Insight, ...]:
    return (Insight("attention", "Example", f"{_readings()} readings wait", "1", "Body."),)


def _asked(provider):
    with projection_scope():
        return provider()


class ProvidedTests(TestCase):
    """What ``provided`` promises of any provider asked through it."""

    def setUp(self):
        derivations.forget()
        self.addCleanup(derivations.forget)

    def test_a_provider_is_computed_once_until_a_table_it_read_is_written(self):
        calls = mock.Mock(side_effect=_counting_items)
        items = provided("extension.example.once.attention", calls)

        first = _asked(items)
        with counting() as (ran, served):
            second = _asked(items)
        self.assertEqual(second, first)
        self.assertEqual((dict(ran), served["extension.example.once.attention"]), ({}, 1))
        self.assertEqual(calls.call_count, 1)

        _write()
        self.assertEqual(_asked(items)[0].title, "1 readings wait")
        self.assertEqual(calls.call_count, 2)

    def test_it_learns_the_tables_it_reads_and_names_none(self):
        items = provided("extension.example.learns.attention", _counting_items)

        _asked(items)

        declared = DERIVATIONS["extension.example.learns.attention"]
        self.assertEqual(declared.reads, ())
        self.assertIn(UpstreamReading._meta.db_table, declared.tables)

    def test_it_stands_a_minute_because_the_host_cannot_see_its_clock(self):
        items = provided("extension.example.unseen.attention", _counting_items)

        with mock.patch.object(derivations, "_clock", return_value=MOMENT), projection_scope():
            items()
            until = derivations.standing(items).until

        self.assertTrue(DERIVATIONS["extension.example.unseen.attention"].unseen)
        self.assertEqual(until, MOMENT + derivations.UNSEEN)

    def test_a_demo_is_answered_apart_from_the_real_reading(self):
        items = provided("extension.example.demo.attention", lambda: (showing_demo(), _readings()))

        real = _asked(items)
        with demo_scope(True):
            shown = _asked(items)
            with counting() as (ran, _served):
                _asked(items)

        self.assertEqual((real, shown), ((False, 0), (True, 0)))
        self.assertEqual(dict(ran), {})

    def test_a_generator_is_kept_as_what_it_yields(self):
        def items():
            yield from _counting_items()

        asked = provided("extension.example.yields.attention", items)

        self.assertEqual(_asked(asked), _counting_items())
        self.assertEqual(_asked(asked), _counting_items())

    def test_a_provider_registered_again_under_one_name_is_never_answered_with_the_other(self):
        first = provided("extension.example.again.dashboard", lambda: ("first",))
        self.assertEqual(_asked(first), ("first",))

        second = provided("extension.example.again.dashboard", lambda: ("second",))

        self.assertIs(second, first)
        self.assertEqual(_asked(second), ("second",))

    def test_an_answer_that_does_not_pickle_is_still_the_answer(self):
        asked = provided("extension.example.unkept.attention", lambda: (lambda: None,))

        with self.assertLogs("severino.derivations", level="ERROR"):
            first = _asked(asked)
        with self.assertLogs("severino.derivations", level="ERROR"), counting() as (ran, _served):
            _asked(asked)

        self.assertTrue(callable(first[0]))
        self.assertEqual(ran["extension.example.unkept.attention"], 1)


def _module_level() -> tuple[()]:
    return ()


class IdentityTests(SimpleTestCase):
    """Which function answers, told so that a stored answer is only ever its own."""

    def test_a_modules_own_function_is_named_by_where_it_is_defined(self):
        self.assertTrue(_identity(_module_level).startswith(f"{__name__}:_module_level:"))

    def test_two_functions_of_one_name_differ_by_what_they_are_written_to_do(self):
        one, other = (lambda: 1), (lambda: 2)  # noqa: E731

        self.assertEqual(one.__qualname__, other.__qualname__)
        self.assertNotEqual(_identity(one), _identity(other))
        self.assertEqual(_identity(one), _identity(one))

    def test_a_closure_built_again_around_the_same_things_is_one_provider(self):
        def build(kind):
            return lambda: kind

        self.assertEqual(_identity(build("running")), _identity(build("running")))
        self.assertNotEqual(_identity(build("running")), _identity(build("cycling")))

    def test_a_partial_is_its_function_and_what_it_was_given(self):
        self.assertEqual(_identity(partial(_module_level, 1)), _identity(partial(_module_level, 1)))
        self.assertNotEqual(_identity(partial(_module_level, 1)), _identity(partial(_module_level, 2)))

    def test_what_cannot_be_compared_is_known_only_as_itself(self):
        def build():
            held = lambda: None  # noqa: E731 - a thing that does not pickle
            return lambda: held

        one, other = mock.Mock(), mock.Mock()
        self.assertEqual(_identity(one), _identity(one))
        self.assertNotEqual(_identity(one), _identity(other))
        self.assertNotEqual(_identity(build()), _identity(build()))

    def test_a_derived_provider_is_answered_by_the_function_behind_it(self):
        asked = provided("extension.example.behind.overview", _module_level)

        self.assertEqual(answered_by(asked), _identity(_module_level))
        self.assertEqual(answered_by(_module_level), _identity(_module_level))
