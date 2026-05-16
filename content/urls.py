from django.urls import path

from . import views

app_name = "content"

urlpatterns = [
    path("", views.ContentListView.as_view(), name="list"),
    path("new/", views.ContentCreateView.as_view(), name="create"),
    path("<slug:slug>/", views.ContentDetailView.as_view(), name="detail"),
    path("<slug:slug>/edit/", views.ContentUpdateView.as_view(), name="edit"),
    path("<slug:slug>/delete/", views.ContentDeleteView.as_view(), name="delete"),
]
