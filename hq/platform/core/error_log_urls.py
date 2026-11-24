from django.urls import path

from .error_log_views import ServerErrorListView, ServerErrorView

app_name = "errors"

urlpatterns = [
    path("", ServerErrorListView.as_view(), name="list"),
    path("<str:request_id>/", ServerErrorView.as_view(), name="detail"),
]
