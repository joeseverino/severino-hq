"""SEVERINO_LOG_LEVEL is every logger's level, so a quiet run prints nothing.

The gates set it to CRITICAL: the suite's tests exercise failures on purpose,
and a logger that kept a level of its own below the root's would print each.
"""

import logging

from django.test import SimpleTestCase


class LogLevelTests(SimpleTestCase):
    def test_no_logger_that_reaches_the_root_is_set_below_it(self):
        # django.server keeps INFO and a handler of its own: runserver's
        # request lines, which no test serves through.
        root = logging.getLogger()
        louder = {
            name: logging.getLevelName(logger.level)
            for name, logger in logging.root.manager.loggerDict.items()
            if isinstance(logger, logging.Logger) and logger.propagate and logging.NOTSET < logger.level < root.level
        }
        self.assertEqual(louder, {}, f"the root is at {logging.getLevelName(root.level)}")
