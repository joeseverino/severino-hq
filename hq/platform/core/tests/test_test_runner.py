"""What a suite run selects: the source tests run once, on a checkout."""

from django.test import SimpleTestCase

from hq.platform.core.test_runner import SOURCE, SeverinoTestRunner, excluded_tags


class SourceSelectionTests(SimpleTestCase):
    def test_the_whole_suite_leaves_the_source_tests_out(self):
        self.assertIn(SOURCE, excluded_tags(None, set(), set()))

    def test_asking_for_the_source_tests_runs_them(self):
        self.assertNotIn(SOURCE, excluded_tags(None, {SOURCE}, set()))

    def test_a_named_label_runs_what_it_names(self):
        self.assertNotIn(SOURCE, excluded_tags(["hq.platform.core"], set(), set()))

    def test_other_exclusions_are_kept(self):
        self.assertIn("slow", excluded_tags(None, set(), {"slow"}))


class EmptySelectionTests(SimpleTestCase):
    def test_a_tag_no_test_carries_fails_the_run(self):
        runner = SeverinoTestRunner(tags=["no-test-carries-this"], verbosity=0)
        with self.assertRaisesMessage(RuntimeError, "no-test-carries-this"):
            runner.build_suite([__name__])
