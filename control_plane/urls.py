from django.urls import path

from . import views

app_name = "control_plane"

urlpatterns = [
    path("", views.InfrastructureListView.as_view(), name="list"),
    path("providers.json", views.ProviderSchemaView.as_view(), name="providers"),
    # Before <slug:key>, which would otherwise swallow "services" as a resource
    # key. The hostname converter is <str:> rather than <slug:> because a
    # hostname has dots in it and a slug does not.
    path("services/", views.ServiceListView.as_view(), name="services"),
    path("new/", views.ResourceFormView.as_view(), name="create"),
    path("services/<str:hostname>/", views.ServiceDetailView.as_view(), name="service"),
    path("adopt/<str:hostname>/", views.AdoptView.as_view(), name="adopt"),
    path("<slug:key>/", views.InfrastructureDetailView.as_view(), name="detail"),
    path("<slug:key>/edit/", views.ResourceFormView.as_view(), name="edit"),
    path("<slug:key>/remove/", views.ResourceRemoveView.as_view(), name="remove"),
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
