"""The controller's standing a test needs, written into the markers the runner gives each test.

The runner starts every class and test with no heartbeat
(``isolation_testing``), so a test that needs a controller says when it last
arrived here, and one that needs none says nothing.
"""

import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import cadence


def controller_arrived(ago: timedelta = timedelta(0)) -> None:
    """The controller last reached this HQ ``ago``: its heartbeat, that old."""

    cadence.note_controller()
    then = (timezone.now() - ago).timestamp()
    os.utime(settings.SEVERINO_CONTROLLER_HEARTBEAT, (then, then))


def controller_never_arrived() -> None:
    """No heartbeat here, whatever the test's setup wrote: only its readings can say a controller ran."""

    Path(settings.SEVERINO_CONTROLLER_HEARTBEAT).unlink(missing_ok=True)
