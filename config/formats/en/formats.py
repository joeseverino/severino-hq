"""How HQ writes dates and times: Oct 3, 2026, 9:29 AM.

A format module rather than DATE_FORMAT in settings. Django always localizes,
and a locale's own formats take precedence over those settings. A
FORMAT_MODULE_PATH module is read ahead of the locale's.

The phrasing is ``application.ui``'s and is only handed on here, so a date a
template prints bare reads as ``|when`` would write it. A page uses ``|when``:
it leaves out the year while it is this one and keeps the instant sortable.
"""

from application.moments import CLOCK_FORMAT, DAY_FORMAT, DAY_YEAR_FORMAT, MOMENT_YEAR_FORMAT

DATE_FORMAT = DAY_YEAR_FORMAT
DATETIME_FORMAT = MOMENT_YEAR_FORMAT
TIME_FORMAT = CLOCK_FORMAT
SHORT_DATE_FORMAT = DAY_YEAR_FORMAT
SHORT_DATETIME_FORMAT = MOMENT_YEAR_FORMAT
MONTH_DAY_FORMAT = DAY_FORMAT
YEAR_MONTH_FORMAT = "F Y"
