from django.test import SimpleTestCase


class FailureDrillTests(SimpleTestCase):
    def test_this_fails_on_purpose(self):
        self.assertEqual(1, 2)
