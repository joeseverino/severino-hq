"""A browser gate runs under Django's parallel workers: a failure there is reported, not lost, a failed
subtest's page is shot as it fails, under the name of its page, width and rule, and a finding reaches
the parent, which fails once for each defect however many places found it."""

import io
import pickle
import threading

from django.test import SimpleTestCase

from hq.platform.core.run_report import CompositionRemoteResult, CompositionTextResult
from hq.platform.core.tests.browser.gate import BrowserGate, failure_name


class Probe(BrowserGate):
    """A gate holding what a real one holds, a browser no pickle can carry.
    Each shot is recorded by name with what the page held when it was taken.

    Its parts are not named test_, so discovery runs none of them on its own.
    """

    def start(self, **options):
        # BrowserGate.setUp runs as it is, cleanups and all, with this in
        # place of the browser it starts.
        self.page = threading.Lock()
        self.on = None
        self.shots = []

    def shoot(self, name):
        self.shots.append((name, self.on))

    def fails(self):
        for page, width in (("example/one", 402), ("example/two", 402), ("example/two", 820)):
            self.on = page
            with self.subTest(rule="overflow", page=page, width=width):
                if page == "example/one":
                    self.fail("on purpose")
                self.fail(f'{{"tables fit": ["table.data-table +{width // 10}px"]}}')
        self.on = "example/last"


class ParallelBrowserGateTests(SimpleTestCase):
    def test_a_failure_reaches_the_parent_shot_on_its_page_and_a_defect_fails_once_at_every_place(self):
        probe = Probe("fails")
        result = CompositionRemoteResult()
        probe.run(result)

        # What the worker sends the parent once the test has finished.
        sent = pickle.loads(pickle.dumps(result.events))
        ((_, _, subtest, (kind, error, _)),) = [event for event in sent if event[0] == "addSubTest"]
        # The failure as the subtest raised it, under the test that ran it.
        self.assertEqual((kind, str(error)), (AssertionError, "on purpose"))
        self.assertTrue(subtest.id().startswith(probe.id()))
        self.assertNotIn("addError", [event[0] for event in sent])
        # A defect is shot where it is first found, never again at another width.
        shots = [
            (failure_name({"page": on, "width": 402, "rule": "overflow"}), on) for on in ("example/one", "example/two")
        ]
        self.assertEqual(probe.shots, shots)
        parent = CompositionTextResult(io.StringIO(), descriptions=False, verbosity=0)
        for event in (event for event in sent if event[0] == "addFindings"):
            parent.addFindings(probe, *event[2:])
        parent.stopTestRun()
        ((_, defect),) = parent.failures
        for said in ("table_region", "example/two at 402px", "example/two at 820px"):
            self.assertIn(said, defect)
