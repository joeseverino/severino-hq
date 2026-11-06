"""A task's script reads its arguments in the shell's grammar, not by the text that looks like a read."""

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.test_pipeline import reads_own_arguments


@tag("source")
class QuotedReadTests(SimpleTestCase):
    def test_a_quoted_program_s_fields_are_not_the_script_s_arguments(self):
        """Single-quoted text is literal in sh, so an awk program's $1 is its field."""
        self.assertFalse(reads_own_arguments("#!/bin/sh\nawk '{print $1}' list\n"))

    def test_a_read_beside_a_quoted_program_still_counts(self):
        self.assertTrue(reads_own_arguments("#!/bin/sh\nawk '{print $1}' \"$@\"\n"))

    def test_an_apostrophe_in_a_comment_hides_no_read(self):
        self.assertTrue(reads_own_arguments('#!/bin/sh\n# the tool\'s first word\nexec tool "$1"\n'))
