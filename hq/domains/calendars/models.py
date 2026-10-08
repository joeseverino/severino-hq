"""What only the calendar knows: the operator's own entries and their choices.

Everything else on the calendar is derived by the domain that holds it.
"""

import uuid
from typing import override

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Length, Trim
from django.db.models.lookups import GreaterThan
from django.urls import reverse

from hq.platform.application.references import ReferenceField
from hq.platform.core.models import TimestampedModel
from hq.platform.core.rules import Rule


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
    # What the event is about, when that is something HQ has a page for.
    about = ReferenceField(
        "about",
        heading="On the calendar",
        shows=("uid", "title", "starts_on", "ends_on", "repeat", "interval", "weekdays", "repeat_until"),
        note="when_note",
    )
    about_name = models.CharField(max_length=200, blank=True, default="", editable=False)

    class Meta:
        ordering = ("starts_on", "starts_at", "title")
        indexes = [models.Index(fields=("starts_on",)), models.Index(fields=("repeat",))]
        constraints = [
            Rule(
                condition=Q(GreaterThan(Length(Trim("title")), 0)),
                name="calendar_entry_has_a_title",
                violation_error_message="Give the entry a title.",
                field="title",
            ),
            Rule(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=F("starts_on")),
                name="calendar_entry_ends_after_it_starts",
                violation_error_message="It cannot end before it starts.",
                field="ends_on",
            ),
            Rule(
                condition=Q(ends_at__isnull=True) | Q(starts_at__isnull=False),
                name="calendar_entry_end_time_has_a_start",
                violation_error_message="An end time needs a start time.",
                field="ends_at",
            ),
            Rule(
                # Within one day the end time follows the start time.
                condition=(
                    Q(ends_at__isnull=True)
                    | Q(starts_at__isnull=True)
                    | Q(ends_on__isnull=False, ends_on__gt=F("starts_on"))
                    | Q(ends_at__gt=F("starts_at"))
                ),
                name="calendar_entry_end_time_follows_start",
                violation_error_message="It cannot end before it starts.",
                field="ends_at",
            ),
            Rule(
                condition=Q(interval__gte=1, interval__lte=366),
                name="calendar_entry_interval_in_range",
                violation_error_message="Repeat every 1 to 366.",
                field="interval",
            ),
            Rule(
                condition=Q(weekdays="") | Q(repeat="weekly"),
                name="calendar_entry_weekdays_only_weekly",
                violation_error_message="Only a weekly entry names its days.",
                field="weekdays",
            ),
            Rule(
                condition=Q(repeat_until__isnull=True) | ~Q(repeat=""),
                name="calendar_entry_until_only_repeating",
                violation_error_message="Only a repeating entry has an end.",
                field="repeat_until",
            ),
            Rule(
                condition=Q(repeat_until__isnull=True) | Q(repeat_until__gte=F("starts_on")),
                name="calendar_entry_until_after_it_starts",
                violation_error_message="It cannot stop repeating before it starts.",
                field="repeat_until",
            ),
        ]

    @override
    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        """An entry is read on the calendar: its day, with it open beside the month."""

        return f"{reverse('calendar:month')}?day={self.starts_on.isoformat()}&entry={self.uid}"

    @property
    def when_note(self) -> str:
        """Its day, or how it repeats, beside its title where something else lists it."""

        from hq.platform.application.calendar_entries import repeat_label
        from hq.platform.application.moments import when_day

        return repeat_label(self) or when_day(self.starts_on)

    @property
    def all_day(self) -> bool:
        return self.starts_at is None

    @property
    def weekday_numbers(self) -> tuple[int, ...]:
        return tuple(sorted({int(day) for day in self.weekdays.split(",") if day.strip()}))

    @override
    def clean(self) -> None:
        """The one rule a check constraint cannot state: what a weekday is.

        Every other rule of an entry is a ``Rule`` in ``Meta.constraints``.
        """

        try:
            days = self.weekday_numbers
        except ValueError:
            days = (-1,)
        if any(day not in range(7) for day in days):
            raise ValidationError({"weekdays": "Weekdays are 0 (Monday) to 6 (Sunday)."})


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

    @override
    def __str__(self) -> str:
        return f"Calendar choices of {self.user}"
