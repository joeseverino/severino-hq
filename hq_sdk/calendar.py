"""The calendar contract: what a domain puts on HQ's calendar.

A domain returns ``CalendarSource`` values from ``PluginIntegration.calendars``.
Each source answers what falls between two days, from what the domain already
holds; the calendar composes every domain's sources and stores none of them.
"""

from application.calendar import CalendarEvent, CalendarSource

__all__ = ["CalendarEvent", "CalendarSource"]
