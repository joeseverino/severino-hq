"""Machine-client routes. Mounted under /api/, which authenticates itself."""

from django.urls import path

from . import views

app_name = "hq_api"
urlpatterns = [
    path("v1/", views.root, {"version": 1}, name="root-v1"),
    path(
        "v1/capabilities/",
        views.capabilities,
        {"version": 1},
        name="capabilities-v1",
    ),
    path(
        "v1/capabilities/<str:name>/",
        views.execute,
        {"version": 1},
        name="execute-v1",
    ),
    path("v2/", views.root, {"version": 2}, name="root"),
    path("v2/capabilities/", views.capabilities, {"version": 2}, name="capabilities"),
    path(
        "v2/capabilities/<str:name>/",
        views.execute,
        {"version": 2},
        name="execute",
    ),
]
