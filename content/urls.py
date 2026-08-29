from django.urls import path

from . import views

app_name = "content"

urlpatterns = [
    path("", views.ContentListView.as_view(), name="list"),
    # Before the slug route, like `new/`: these are sections, not items.
    path("writeups/", views.WriteupListView.as_view(), name="writeups"),
    path("pages/", views.PageListView.as_view(), name="pages"),
    path("new/", views.ContentCreateView.as_view(), name="create"),
    path("<slug:slug>/", views.ContentDetailView.as_view(), name="detail"),
    path("<slug:slug>/edit/", views.ContentUpdateView.as_view(), name="edit"),
    path("<slug:slug>/delete/", views.ContentDeleteView.as_view(), name="delete"),
]
