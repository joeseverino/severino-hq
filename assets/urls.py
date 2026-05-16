from django.urls import path

from . import views

app_name = "assets"

urlpatterns = [
    path("", views.AssetListView.as_view(), name="list"),
    path("new/", views.AssetCreateView.as_view(), name="create"),
    path("<slug:slug>/", views.AssetDetailView.as_view(), name="detail"),
    path("<slug:slug>/edit/", views.AssetUpdateView.as_view(), name="edit"),
    path("<slug:slug>/delete/", views.AssetDeleteView.as_view(), name="delete"),
]
