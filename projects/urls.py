from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("", views.ProjectListView.as_view(), name="list"),
    path("new/", views.ProjectCreateView.as_view(), name="create"),
    path("<slug:slug>/", views.ProjectDetailView.as_view(), name="detail"),
    path("<slug:slug>/refresh/", views.ProjectRefreshView.as_view(), name="refresh"),
    path("<slug:slug>/edit/", views.ProjectUpdateView.as_view(), name="edit"),
    path("<slug:slug>/delete/", views.ProjectDeleteView.as_view(), name="delete"),
]
