from django.urls import path

from .views import JobListView, JobStatusView

app_name = "jobs"

urlpatterns = [
    path("", JobListView.as_view(), name="list"),
    # Polled by the page that started the job. Deliberately its own endpoint
    # returning only the row's state: the page it was started from may render
    # a great deal, and asking for all of that every two seconds to learn one
    # number is how a progress bar becomes the most expensive thing on a host.
    path("<uuid:pk>.json", JobStatusView.as_view(), name="status"),
]
