"""How HQ writes dates and times: 5/23/26, 5:49 PM.

A format module rather than DATE_FORMAT in settings. Django always localizes,
and a locale's own formats take precedence over those settings, so the
settings were ignored and every page wrote "Sept. 25, 2026, 5:32 p.m.". A
FORMAT_MODULE_PATH module is read ahead of the locale's.
"""

DATE_FORMAT = "n/j/y"
DATETIME_FORMAT = "n/j/y g:i A"
TIME_FORMAT = "g:i A"
SHORT_DATE_FORMAT = "n/j/y"
SHORT_DATETIME_FORMAT = "n/j/y g:i A"
MONTH_DAY_FORMAT = "M j"
YEAR_MONTH_FORMAT = "F Y"
