from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from hq_sdk.pages import page_context

from .models import Note


def index(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "example_extension/index.html",
        {
            **page_context("Example extension", "A page an extension draws from the host's primitives."),
            "notes": Note.objects.all(),
        },
    )


def note(request: HttpRequest, slug: str) -> HttpResponse:
    found = get_object_or_404(Note, slug=slug)
    return render(
        request,
        "example_extension/note.html",
        {**page_context(found.title, "One note of the example extension's."), "note": found},
    )
