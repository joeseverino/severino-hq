"""A request cannot wait on anything outside the process, whatever it calls."""

from django.test import SimpleTestCase

from hq.platform.core import outbound
from hq.platform.core.outbound_allowed import allowed


class RefusalTests(SimpleTestCase):
    def test_an_exception_nobody_declared_is_not_one(self):
        with self.assertRaises(ValueError), allowed("because-i-say-so"):
            pass

    def test_the_default_is_to_refuse(self):
        self.assertEqual(outbound.mode(), outbound.REFUSE)
