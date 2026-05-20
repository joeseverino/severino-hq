from django.urls import path

from . import views

app_name = "contacts"

urlpatterns = [
    path("", views.contact_list, name="list"),
    path("<int:pk>/", views.contact_detail, name="detail"),
]
