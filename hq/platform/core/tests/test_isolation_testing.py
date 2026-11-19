"""A test class starts from what a fresh process holds, whatever the class run before it left.

The runner isolates the adopted catalog, the agent limits and the controller's
markers per test (``isolation_testing``). A class's own setup runs before its
first test, so it is held to the same: a class that stopped in its setup,
wrote in its teardown, or left a composition cached that a patch had narrowed,
leaves nothing for the next class to set up on.
"""

import io
import logging
import unittest
import unittest.mock

from django.test import TestCase

from hq.platform.application import cadence
from hq.platform.application.automation_runs import runs_procedure
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.security import web_principal
from hq.platform.core.example_estate import automation
from hq.platform.core.test_runner import SeverinoTestRunner
from hq.platform.core.testing import operator


def _run(*tests: unittest.TestCase, **options: bool) -> unittest.TestResult:
    """``tests`` in one process, in order, as the runner runs a worker's share.

    Each is a plain ``unittest.TestCase``, so it runs inside this test's
    transaction and its rows are this test's. ``options`` are the runner's
    (``debug_sql``), each of which may choose a result of Django's own.
    """

    runner = SeverinoTestRunner(verbosity=0, **options)
    suite = runner.test_suite(tests)
    text = unittest.TextTestRunner(stream=io.StringIO(), resultclass=runner.get_resultclass(), verbosity=0)
    # Django's debug_sql result sets its logger to DEBUG for good; the
    # process this runs in keeps its own level.
    queries = logging.getLogger("django.db.backends")
    level = queries.level
    try:
        return text.run(suite)
    finally:
        queries.setLevel(level)


def _procedures() -> set[str]:
    return {name for name in capability_registry() if runs_procedure(name)}


class ClassSetupStartsFreshTests(TestCase):
    def test_a_class_sets_up_on_none_of_what_a_class_that_stopped_in_its_setup_adopted_or_heard(self):
        seen: dict[str, object] = {}

        class Adopts(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                automation.adopted(web_principal(operator()))
                cadence.note_controller()
                raise unittest.SkipTest("stops in its setup, so no test of it starts or stops")

            def test_nothing(self):
                pass

        class Reads(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                seen["procedures"] = _procedures()
                seen["heartbeat"] = cadence.controller_standing().here

            def test_reads_what_its_setup_read(self):
                self.assertEqual(_procedures(), seen["procedures"])

        result = _run(Adopts("test_nothing"), Reads("test_reads_what_its_setup_read"))

        self.assertEqual(seen, {"procedures": set(), "heartbeat": False})
        self.assertEqual([said for _, said in result.failures + result.errors], [])

    def test_a_class_sets_up_on_none_of_what_the_class_before_it_wrote_in_its_teardown(self):
        seen: dict[str, object] = {}

        class Hears(unittest.TestCase):
            @classmethod
            def tearDownClass(cls):
                cadence.note_controller()
                super().tearDownClass()

            def test_nothing(self):
                pass

        class Reads(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                seen["heartbeat"] = cadence.controller_standing().here

            def test_nothing(self):
                pass

        result = _run(Hears("test_nothing"), Reads("test_nothing"))

        self.assertEqual([said for _, said in result.failures + result.errors], [])
        self.assertEqual(seen, {"heartbeat": False})

    def test_a_test_reads_none_of_what_the_test_before_it_wrote_whichever_result_the_runner_chose(self):
        seen: dict[str, bool] = {}

        class Hears(unittest.TestCase):
            def test_1_hears_the_controller(self):
                cadence.note_controller()

            def test_2_reads(self):
                seen["heartbeat"] = cadence.controller_standing().here

        for options in ({}, {"debug_sql": True}):
            with self.subTest(**options):
                seen.clear()

                result = _run(Hears("test_1_hears_the_controller"), Hears("test_2_reads"), **options)

                self.assertEqual([said for _, said in result.failures + result.errors], [])
                self.assertEqual(seen, {"heartbeat": False})

    def test_a_class_sets_up_on_the_composition_as_it_is_not_as_a_patch_before_it_left_it_cached(self):
        seen: dict[str, set[str]] = {}

        class Narrows(unittest.TestCase):
            def test_reads_its_commands_without_the_records(self):
                with unittest.mock.patch(
                    "hq.platform.application.integrations.record_capability_specs", return_value=()
                ):
                    capability_registry()

        class Reads(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                seen["set up"] = set(capability_registry())

            def test_reads_its_commands(self):
                seen["tested"] = set(capability_registry())

        result = _run(Narrows("test_reads_its_commands_without_the_records"), Reads("test_reads_its_commands"))

        self.assertEqual([said for _, said in result.failures + result.errors], [])
        self.assertEqual(seen["set up"], seen["tested"])
