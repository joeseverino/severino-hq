from django.urls import path

from .views import JobListView, RunView, WorkAskView

app_name = "jobs"

urlpatterns = [
    path("", JobListView.as_view(), name="list"),
    path("runs/<uuid:pk>/", RunView.as_view(), name="run"),
    # Where the control of a declared piece of outbound work posts. The name is
    # the declaration's; the route exists for whatever the composition declares.
    path("ask/<str:name>/", WorkAskView.as_view(), name="ask"),
]
