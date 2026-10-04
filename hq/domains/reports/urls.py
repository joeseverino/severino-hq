from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportsView.as_view(), name="dashboard"),
    # Derived from the one declaration of what an export is, so a new report is
    # a row in `EXPORTS` rather than a view, a route and a filename convention
    # that have to be kept agreeing with each other.
    *(
        path(export.path, views.ExportView.as_view(export=export), name=export.name)
        for export in views.EXPORTS
    ),
]
