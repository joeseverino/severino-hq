from django.urls import path

from .views import index

app_name = "example_plugin"
urlpatterns = [path("", index, name="index")]
