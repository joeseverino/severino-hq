from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from hq_sdk.pages import page_context

ITEMS = ("example.one", "example.two")


def index(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "example_extension/index.html",
        {**page_context("Example extension", "A page an extension draws from the host's primitives."), "items": ITEMS},
    )
