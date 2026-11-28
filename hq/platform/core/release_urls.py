from django.urls import path

from .release_views import ReleaseListView, ReleaseView

app_name = "releases"

urlpatterns = [
    path("", ReleaseListView.as_view(), name="list"),
    path("<str:commit>/", ReleaseView.as_view(), name="detail"),
]
