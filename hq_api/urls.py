"""Machine-client routes. Mounted under /api/, which authenticates itself."""

from django.urls import path

from . import views

app_name = "hq_api"
urlpatterns = [
    path("v1/", views.root, name="root"),
    path("v1/capabilities/", views.capabilities, name="capabilities"),
    path("v1/capabilities/<str:name>/", views.execute, name="execute"),
]
