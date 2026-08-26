from django.urls import path

from . import views
from .models import OperationRequest

app_name = "control_plane"

urlpatterns = [
    path("", views.InfrastructureListView.as_view(), name="list"),
    path("findings/", views.FindingsView.as_view(), name="findings"),
    path("topology/", views.TopologyView.as_view(), name="topology"),
    path("providers.json", views.ProviderSchemaView.as_view(), name="providers"),
    # Before <slug:key>, which would otherwise swallow "services" as a resource
    # key. The hostname converter is <str:> rather than <slug:> because a
    # hostname has dots in it and a slug does not.
    path("services/", views.ServiceListView.as_view(), name="services"),
    path(
        "connections/",
        views.ConnectionListView.as_view(),
        name="connections",
    ),
    # Before <slug:key>, which would otherwise swallow "tools" as a resource key.
    path("tools/", views.ToolsView.as_view(), name="tools"),
    path("machines/", views.MachineListView.as_view(), name="machines"),
    path("tailnet/", views.TailnetView.as_view(), name="tailnet"),
    # Before <slug:key>, which would otherwise swallow a machine name.
    path("machines/<str:name>/", views.MachineDetailView.as_view(), name="machine"),
    path(
        "services/<str:hostname>/pin/",
        views.ServicePinView.as_view(),
        name="service_pin",
    ),
    path(
        "services/<str:hostname>/move/",
        views.ServiceMoveView.as_view(),
        name="service_move",
    ),
    # Before <str:hostname>, which would otherwise swallow "new" as a name.
    path("services/new/", views.ServiceStartView.as_view(), name="service_start"),
    path("new/", views.ResourceFormView.as_view(), name="create"),
    path("services/<str:hostname>/", views.ServiceDetailView.as_view(), name="service"),
    path("adopt/<str:hostname>/", views.AdoptView.as_view(), name="adopt"),
    # One specific record rather than everything a hostname answers with. A
    # container has no hostname at all, so it is unreachable from the route
    # above and would otherwise be adoptable only through the API.
    path(
        "adopt/record/<str:kind>/<str:token>/",
        views.AdoptRecordView.as_view(),
        name="adopt_record",
    ),
    path("<slug:key>/", views.InfrastructureDetailView.as_view(), name="detail"),
    path("<slug:key>/edit/", views.ResourceFormView.as_view(), name="edit"),
    path("<slug:key>/remove/", views.ResourceRemoveView.as_view(), name="remove"),
    path(
        "<slug:key>/certificate/",
        views.CertificateUploadView.as_view(),
        name="upload_certificate",
    ),
    path(
        "<slug:key>/reconcile/",
        views.OperationView.as_view(action=OperationRequest.Action.RECONCILE),
        name="reconcile",
    ),
    path(
        "<slug:key>/renew/",
        views.OperationView.as_view(action=OperationRequest.Action.RENEW),
        name="renew",
    ),
    # Lifecycle verbs, one route each. The view takes its action from the URL,
    # so a verb is a route and a phrase rather than another view doing what this
    # one already does.
    path(
        "<slug:key>/restart/",
        views.OperationView.as_view(action=OperationRequest.Action.RESTART),
        name="restart",
    ),
    path(
        "<slug:key>/start/",
        views.OperationView.as_view(action=OperationRequest.Action.START),
        name="start",
    ),
    path(
        "<slug:key>/stop/",
        views.OperationView.as_view(action=OperationRequest.Action.STOP),
        name="stop",
    ),
    path(
        "<slug:key>/approve-routes/",
        views.OperationView.as_view(action=OperationRequest.Action.APPROVE_ROUTES),
        name="approve_routes",
    ),
    path(
        "<slug:key>/certificate.pem",
        views.CertificateDownloadView.as_view(),
        name="certificate_download",
    ),
    path(
        "<slug:key>/report.json",
        views.ResourceReportDownloadView.as_view(),
        name="report_download",
    ),
]
