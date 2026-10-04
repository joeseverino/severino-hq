from django.urls import path

from . import views

app_name = "receipts"

urlpatterns = [
    path("", views.ReceiptListView.as_view(), name="list"),
    path("new/", views.ReceiptCreateView.as_view(), name="create"),
    path("<int:pk>/", views.ReceiptDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.ReceiptUpdateView.as_view(), name="edit"),
    path("<int:pk>/match/", views.ReceiptMatchView.as_view(), name="match"),
    path("<int:pk>/delete/", views.ReceiptDeleteView.as_view(), name="delete"),
    path("<int:pk>/file/", views.ReceiptFileView.as_view(), name="file"),
]
