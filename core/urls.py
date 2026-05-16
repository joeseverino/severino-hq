from django.urls import path

from .views import AuditLogListView

app_name = "core"

urlpatterns = [
    path("", AuditLogListView.as_view(), name="audit_list"),
]
