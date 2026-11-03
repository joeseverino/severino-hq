"""Every door HQ declares has a name of its own and words beside who came through it."""

from django.test import SimpleTestCase

from hq.platform.application import security
from hq.platform.application.doors import DOORS, LABELS


class DoorWordsTests(SimpleTestCase):
    def test_every_declared_interface_has_words(self):
        declared = {value for name, value in vars(security).items() if name.endswith("_INTERFACE")}

        self.assertTrue(declared)
        self.assertEqual(declared - set(DOORS), set())
        self.assertEqual(declared - set(LABELS), set())
