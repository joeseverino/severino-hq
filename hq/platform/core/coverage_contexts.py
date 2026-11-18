"""A coverage context for each test and each test module's fixtures, which
fast's map of the tests that ran each line (``tools/gate/testmap.py``) and the
audit of tests whose lines others cover (``tools/audit_tests.py``) both read.

The module's context is switched to before its fixtures, so what its class
fixtures run is its own, in a worker as in a serial run: Django gives each
worker one class's tests at a time, and each such suite opens on its module's
fixture. Each test's own context follows its class's setup, so what the test
and its ``setUp`` run is the test's. A test's context names its module before
``SEPARATOR``, so a reader that wants modules reads them back with ``module_of``.
"""

import sys
import unittest

# The core the run that records the map asks for (`fast:tests --map`). Under
# sys.monitoring, coverage's default, a line is recorded once per process, so
# a later module that runs it would not be told; the suite's own coverage run
# keeps that core and no contexts.
TRACER = "ctrace"

# Between a test's module and the rest of its id. A module's name holds dots,
# and so may a class's qualified name, so only a character no Python name
# holds splits the two.
SEPARATOR = ":"


def module_of(context: str) -> str:
    """The test module whose fixtures or test a context recorded."""

    return context.partition(SEPARATOR)[0]


def label_of(context: str) -> str:
    """A test's context as the label ``manage.py test`` takes (``module.Class.test``)."""

    return context.replace(SEPARATOR, ".", 1)


def _measuring():
    # coverage is in sys.modules only when it measures the run; the host's
    # own code never imports it.
    coverage = sys.modules.get("coverage")
    measuring = coverage.Coverage.current() if coverage else None
    return measuring if measuring and measuring.get_option("run:core") == TRACER else None


class TestContexts(unittest.TestSuite):
    def _handleModuleFixture(self, test, result):
        if measuring := _measuring():
            measuring.switch_context(type(test).__module__)
        super()._handleModuleFixture(test, result)

    def _handleClassSetUp(self, test, result):
        super()._handleClassSetUp(test, result)
        # A loader's stand-in for a module that failed to import has no method of its own.
        method = getattr(test, "_testMethodName", None)
        if method and (measuring := _measuring()):
            cls = type(test)
            measuring.switch_context(f"{cls.__module__}{SEPARATOR}{cls.__qualname__}.{method}")
