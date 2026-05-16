from django.urls import path

from . import views

app_name = "docs_index"

urlpatterns = [
    path("", views.DocsListView.as_view(), name="list"),
    path("new/", views.DocsCreateView.as_view(), name="create"),
    path("import/", views.ManifestImportView.as_view(), name="import"),
    path("<slug:doc_id>/", views.DocsDetailView.as_view(), name="detail"),
    path("<slug:doc_id>/edit/", views.DocsUpdateView.as_view(), name="edit"),
    path("<slug:doc_id>/delete/", views.DocsDeleteView.as_view(), name="delete"),
]
