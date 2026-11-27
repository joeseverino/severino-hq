"""An extension's part of a shared composition, derived once per change by the host."""

from functools import partial
from unittest import mock

from django.test import SimpleTestCase

from hq.platform.application.composed_providers import _identity


def _module_level() -> tuple[()]:
    return ()


class IdentityTests(SimpleTestCase):
    """Which function answers, told so that a stored answer is only ever its own."""

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
