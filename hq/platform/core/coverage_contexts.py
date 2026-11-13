"""A coverage context for each test module, which fast's map of the tests
that ran each file is read from (``tools/gate/testmap.py``).

Switched before a module's fixtures, so what its class fixtures and its tests
run is its own, in a worker as in a serial run: Django gives each worker one
class's tests at a time, and each such suite opens on its module's fixture.
"""

import sys
import unittest

# The core the run that records the map asks for (`fast:tests --map`). Under
# sys.monitoring, coverage's default, a line is recorded once per process, so
# a later module that runs it would not be told; the suite's own coverage run
# keeps that core and no contexts.
TRACER = "ctrace"


class ModuleContexts(unittest.TestSuite):
    def _handleModuleFixture(self, test, result):
        # coverage is in sys.modules only when it measures the run; the host's
        # own code never imports it.
        coverage = sys.modules.get("coverage")
        measuring = coverage.Coverage.current() if coverage else None
        if measuring and measuring.get_option("run:core") == TRACER:
            measuring.switch_context(type(test).__module__)
        super()._handleModuleFixture(test, result)
