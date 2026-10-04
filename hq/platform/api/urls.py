"""Machine-client routes. Mounted under /api/, which authenticates itself."""

from django.urls import path

from . import views

app_name = "hq_api"
urlpatterns = [
    path("v2/", views.root, name="root"),
    path("v2/openapi.json", views.openapi, name="openapi"),
    path("v2/capabilities/", views.capabilities, name="capabilities"),
    path("v2/resources/", views.resources, name="resources"),
    path("v2/connections/", views.connections, name="connections"),
    path("v2/topology/", views.topology, name="topology"),
    path("v2/findings/", views.findings, name="findings"),
    path("v2/resources/<str:name>/", views.resource_list, name="resource-list"),
    path(
        "v2/resources/<str:name>/<str:identifier>/",
        views.resource_detail,
        name="resource-detail",
    ),
    path("v2/capabilities/<str:name>/", views.execute, name="execute"),
]
