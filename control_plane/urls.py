from django.urls import path

from . import approval_views, connection_views, exposure_views, finding_views, machine_views, resource_form_views, service_views, timeline_views, tool_views, topology_views, views
from .container_views import ContainerListView
from .models import OperationRequest

app_name = "control_plane"

urlpatterns = [
    path("", views.InfrastructureListView.as_view(), name="list"),
    path("findings/", finding_views.FindingsView.as_view(), name="findings"),
    path("exposure/", exposure_views.ExposureView.as_view(), name="exposure"),
    path("timeline/", timeline_views.TimelineView.as_view(), name="timeline"),
    path("topology/", topology_views.TopologyView.as_view(), name="topology"),
    path("topology/node/", topology_views.TopologyNodeView.as_view(), name="topology_node"),
    path("providers.json", views.ProviderSchemaView.as_view(), name="providers"),
    # Before <slug:key>, which would otherwise swallow "services" as a resource
    # key. The hostname converter is <str:> rather than <slug:> because a
    # hostname has dots in it and a slug does not.
    path("services/", service_views.ServiceListView.as_view(), name="services"),
    path(
        "connections/",
        connection_views.ConnectionListView.as_view(),
        name="connections",
    ),
    path("connections/read/", connection_views.ReadNowView.as_view(), name="read_now"),
    # Before <slug:key>, which would otherwise swallow "approvals" as a
    # resource key.
    path("approvals/", approval_views.ApprovalListView.as_view(), name="approvals"),
    path(
        "approvals/<uuid:approval_id>/",
        approval_views.ApprovalDecisionView.as_view(),
        name="approval_decision",
    ),
    path(
        "approvals/<uuid:approval_id>/<str:decision>/",
        approval_views.ApprovalDecisionView.as_view(),
        name="approval_decide",
    ),
    # Before <slug:key>, which would otherwise swallow "tools" as a resource key.
    path("tools/", tool_views.ToolsView.as_view(), name="tools"),
    path("machines/", machine_views.MachineListView.as_view(), name="machines"),
    path("tailnet/", machine_views.TailnetView.as_view(), name="tailnet"),
    # Before <slug:key>, which would otherwise swallow a machine name.
    path("machines/<str:name>/", machine_views.MachineDetailView.as_view(), name="machine"),
    path(
        "services/<str:hostname>/pin/",
        service_views.ServicePinView.as_view(),
        name="service_pin",
    ),
    path(
        "services/<str:hostname>/move/",
        service_views.ServiceMoveView.as_view(),
        name="service_move",
    ),
    # Before <str:hostname>, which would otherwise swallow "new" as a name.
    path("services/new/", service_views.ServiceStartView.as_view(), name="service_start"),
    path("new/", resource_form_views.ResourceFormView.as_view(), name="create"),
    path("services/<str:hostname>/", service_views.ServiceDetailView.as_view(), name="service"),
    path("adopt/<str:hostname>/", resource_form_views.AdoptView.as_view(), name="adopt"),
    # One specific record rather than everything a hostname answers with. A
    # container has no hostname at all, so it is unreachable from the route
    # above and would otherwise be adoptable only through the API.
    path(
        "adopt/record/<str:kind>/<str:token>/",
        resource_form_views.AdoptRecordView.as_view(),
        name="adopt_record",
    ),
    path("containers/", ContainerListView.as_view(), name="containers"),
    path("<slug:key>/", views.InfrastructureDetailView.as_view(), name="detail"),
    path("<slug:key>/edit/", resource_form_views.ResourceFormView.as_view(), name="edit"),
    path("<slug:key>/remove/", views.ResourceRemoveView.as_view(), name="remove"),
    path(
        "<slug:key>/certificate/",
        resource_form_views.CertificateUploadView.as_view(),
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
