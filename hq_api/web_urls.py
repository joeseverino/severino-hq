"""Session pages about the machine API, mounted by the ``hq.api`` domain."""

from django.urls import path

from .reference import reference

app_name = "api_reference"
urlpatterns = [path("", reference, name="reference")]
