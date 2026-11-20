from django.urls import path

from . import views

app_name = "contacts"

urlpatterns = [
    path("", views.ContactListView.as_view(), name="list"),
    path("<int:pk>/", views.ContactDetailView.as_view(), name="detail"),
    path("<int:pk>/status/", views.contact_set_status, name="set_status"),
    path("<int:pk>/delete/", views.contact_delete, name="delete"),
]
