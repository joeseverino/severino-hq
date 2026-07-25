from django.urls import path

from . import views

app_name = "control_plane"

urlpatterns = [
    path("", views.InfrastructureListView.as_view(), name="list"),
    path("providers.json", views.ProviderSchemaView.as_view(), name="providers"),
    path("<slug:key>/", views.InfrastructureDetailView.as_view(), name="detail"),
    path("<slug:key>/reconcile/", views.ReconcileView.as_view(), name="reconcile"),
    path(
        "<slug:key>/renew/",
        views.RenewCertificateView.as_view(),
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
