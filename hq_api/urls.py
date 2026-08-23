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
    path("v2/resources/", views.resources, {"version": 2}, name="resources"),
    path("v2/connections/", views.connections, {"version": 2}, name="connections"),
    path("v2/topology/", views.topology, {"version": 2}, name="topology"),
    path(
        "v2/resources/<str:name>/",
        views.resource_list,
        {"version": 2},
        name="resource-list",
    ),
    path(
        "v2/resources/<str:name>/<str:identifier>/",
        views.resource_detail,
        {"version": 2},
        name="resource-detail",
    ),
    path(
        "v2/capabilities/<str:name>/",
        views.execute,
        {"version": 2},
        name="execute",
    ),
]
