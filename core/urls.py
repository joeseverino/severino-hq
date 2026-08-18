from django.urls import path

from .views import AuditLogDetailView, AuditLogListView

app_name = "core"

urlpatterns = [
    path("", AuditLogListView.as_view(), name="audit_list"),
    path("<int:pk>/", AuditLogDetailView.as_view(), name="audit_detail"),
]
