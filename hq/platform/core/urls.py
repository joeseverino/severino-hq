from django.urls import path

from .audit_views import AuditLogDetailView, AuditLogListView
from .views import ApprovalEntryView

app_name = "core"

urlpatterns = [
    path("", AuditLogListView.as_view(), name="audit_list"),
    path("<int:pk>/", AuditLogDetailView.as_view(), name="audit_detail"),
    path("approval/<uuid:approval_id>/", ApprovalEntryView.as_view(), name="approval_entry"),
]
