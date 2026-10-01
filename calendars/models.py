"""What only the calendar knows: the operator's own entries and their choices.

Everything else on the calendar is derived by the domain that holds it.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse

from core.models import TimestampedModel


class Entry(TimestampedModel):
    """One entry on My Calendar, in the operator's own time zone.

    A day without times is all day. ``ends_on`` is the last day it covers,
    inclusive, as a person says it ("the 3rd to the 5th"); the calendar turns it
    into iCalendar's exclusive end where it needs one.
    """

    class Repeat(models.TextChoices):
        NONE = "", "Does not repeat"
        DAILY = "daily", "Every day"
        WEEKLY = "weekly", "Every week"
        MONTHLY = "monthly", "Every month"
        YEARLY = "yearly", "Every year"

    # The iCalendar UID: stable for the entry's life, so a feed or a synced
    # calendar recognises an edit as the same event.
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=160)
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    starts_at = models.TimeField(null=True, blank=True)
    ends_at = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    repeat = models.CharField(max_length=10, choices=Repeat.choices, blank=True, default="")
    interval = models.PositiveSmallIntegerField(default=1)
    # Monday is 0. Only a weekly entry names its days; blank means the day it starts.
    weekdays = models.CharField(max_length=20, blank=True)
    repeat_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ("starts_on", "starts_at", "title")
        indexes = [models.Index(fields=("starts_on",)), models.Index(fields=("repeat",))]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        """An entry is read on the calendar: its day, with it open beside the month."""

        return f"{reverse('calendar:month')}?day={self.starts_on.isoformat()}&entry={self.uid}"

    @property
    def all_day(self) -> bool:
        return self.starts_at is None

    @property
    def weekday_numbers(self) -> tuple[int, ...]:
        return tuple(sorted({int(day) for day in self.weekdays.split(",") if day.strip()}))

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if not self.title.strip():
            errors["title"] = "Give the entry a title."
        if self.ends_on and self.ends_on < self.starts_on:
            errors["ends_on"] = "It cannot end before it starts."
        if self.ends_at and not self.starts_at:
            errors["ends_at"] = "An end time needs a start time."
        if (
            self.starts_at
            and self.ends_at
            and (self.ends_on or self.starts_on) == self.starts_on
            and self.ends_at <= self.starts_at
        ):
            errors["ends_at"] = "It cannot end before it starts."
        if not 1 <= self.interval <= 366:
            errors["interval"] = "Repeat every 1 to 366."
        try:
            days = self.weekday_numbers
        except ValueError:
            days = (-1,)
        if any(day not in range(7) for day in days):
            errors["weekdays"] = "Weekdays are 0 (Monday) to 6 (Sunday)."
        elif days and self.repeat != self.Repeat.WEEKLY:
            errors["weekdays"] = "Only a weekly entry names its days."
        if self.repeat_until and not self.repeat:
            errors["repeat_until"] = "Only a repeating entry has an end."
        elif self.repeat_until and self.repeat_until < self.starts_on:
            errors["repeat_until"] = "It cannot stop repeating before it starts."
        if errors:
            raise ValidationError(errors)


class Preference(TimestampedModel):
    """Which sources one operator has checked or unchecked.

    Kept as the choice made, source by source, not as a hidden list: a source
    that starts unchecked and one that starts checked are then told apart, and
    a new source arrives in its own default state.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_preference"
    )
    choices = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Calendar choices of {self.user}"
