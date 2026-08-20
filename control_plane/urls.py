from django.urls import path

from . import views
from .models import OperationRequest

app_name = "control_plane"

urlpatterns = [
    path("", views.InfrastructureListView.as_view(), name="list"),
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
    # Before <str:hostname>, which would otherwise swallow "new" as a name.
    path("services/new/", views.ServiceStartView.as_view(), name="service_start"),
    path("new/", views.ResourceFormView.as_view(), name="create"),
    path("services/<str:hostname>/", views.ServiceDetailView.as_view(), name="service"),
    path("adopt/<str:hostname>/", views.AdoptView.as_view(), name="adopt"),
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
