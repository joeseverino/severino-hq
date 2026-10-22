"""A browser gate runs under Django's parallel workers: a failure there is
reported, not lost, and a failed subtest's page is shot as it fails, under the
name of its page, width and rule."""

import pickle
import threading

from django.test import SimpleTestCase
from django.test.runner import RemoteTestResult

from hq.platform.core.browser_tests import BrowserGate, failure_name


class Probe(BrowserGate):
    """A gate holding what a real one holds, a browser no pickle can carry.
    Each shot is recorded by name with what the page held when it was taken.

    Its parts are not named test_, so discovery runs none of them on its own.
    """

    def setUp(self):
        self.page = threading.Lock()
        self.on = None
        self.shots = []
        # The cleanups BrowserGate.setUp registers, in its order.
        self.addCleanup(lambda: None)
        self.addCleanup(self.capture_failure)

    def shoot(self, name):
        self.shots.append((name, self.on))

    def fails(self):
        for page in ("example/one", "example/two"):
            self.on = page
            with self.subTest(rule="overflow", page=page, width=402):
                if page == "example/one":
                    self.fail("on purpose")
        self.on = "example/last"

    def passes(self):
        with self.subTest(part="one"):
            pass

    def breaks(self):
        self.on = "example/last"
        self.fail("outside a subtest")


class ParallelBrowserGateTests(SimpleTestCase):
    def test_a_failed_subtest_reaches_the_parent_and_is_shot_on_its_own_page(self):
        probe = Probe("fails")
        result = RemoteTestResult()
        probe.run(result)

        # What the worker sends the parent once the test has finished.
        sent = pickle.loads(pickle.dumps(result.events))
        self.assertIn("addSubTest", [event[0] for event in sent])
        self.assertEqual(
            probe.shots, [(failure_name({"page": "example/one", "width": 402, "rule": "overflow"}), "example/one")]
        )

    def test_a_test_failing_outside_its_subtests_is_shot_as_it_ended(self):
        probe = Probe("breaks")
        probe.run(RemoteTestResult())

        self.assertTrue(probe.failed)
        self.assertEqual(probe.shots, [("page", "example/last")])

    def test_a_passing_test_is_not_shot(self):
        probe = Probe("passes")
        probe.run(RemoteTestResult())

        self.assertFalse(probe.failed)
        self.assertEqual(probe.shots, [])

    def test_a_shot_is_named_for_its_page_width_and_rule_as_one_file_name(self):
        name = failure_name({"rule": "overflow", "theme": "dark", "width": 402, "page": "dense/services"})
        self.assertEqual(name, "dense-services-402-overflow-dark")
        self.assertNotIn("/", failure_name({"page": "estate/a/b?c=d"}))
        self.assertLessEqual(len(failure_name({"said": "x" * 400})), 150)
