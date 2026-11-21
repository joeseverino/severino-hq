from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from hq_sdk.pages import page_context
from hq_sdk.tables import Cell, DataTable, Row, TableColumn, record_link

from .models import Note


def _row(note: Note) -> Row:
    """A note by its page, and the day it was written with the slug it is filed as."""

    filed = Cell(f"filed as {note.slug}", shows="more", dot=True)
    return Row((Cell.of(record_link(note)), Cell(moment=note.written_on, form="day", parts=(filed,))))


def index(request: HttpRequest) -> HttpResponse:
    notes = DataTable("Notes", (TableColumn("Note"), TableColumn("Written")), tuple(map(_row, Note.objects.all())))
    return render(
        request,
        "example_extension/index.html",
        {**page_context("Example extension", "A page an extension draws from the host's primitives."), "notes": notes},
    )


def note(request: HttpRequest, slug: str) -> HttpResponse:
    found = get_object_or_404(Note, slug=slug)
    return render(
        request,
        "example_extension/note.html",
        {**page_context(found.title, "One note of the example extension's."), "note": found},
    )
