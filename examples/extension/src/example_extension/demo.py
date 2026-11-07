"""The extension's demo records, which HQ's showcase and standard data sets write.

Each note is dated back from the day it is written, so the demo never goes
stale, and keyed by its slug, so every database it writes holds the same pages.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .models import Note

# Slug, title and how many days ago it was written.
NOTES = (
    ("first-steps", "First steps", 30),
    ("a-week-in", "A week in", 7),
    ("today", "Today", 0),
)


def plant(owner: object) -> None:
    today = timezone.localdate()
    Note.objects.bulk_create(
        Note(slug=slug, title=title, written_on=today - timedelta(days=ago)) for slug, title, ago in NOTES
    )


def pages() -> tuple[str, ...]:
    """A note's page, the one kind of record the demo writes."""

    return (reverse("example_extension:note", kwargs={"slug": NOTES[0][0]}),)
