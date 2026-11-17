"""audit:image refuses an image word docker could read as a flag."""

from django.test import SimpleTestCase

from tools import audit_image


class ReferenceTests(SimpleTestCase):
    def test_a_word_docker_could_read_as_a_flag_is_refused_before_docker_runs(self) -> None:
        for word in ("--output=/etc/x", "-o", "a b", ""):
            with self.subTest(word=word):
                self.assertEqual(audit_image.main(word), 2)
