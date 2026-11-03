from django.urls import path

from .views import index

app_name = "example_extension"
urlpatterns = [path("", index, name="index")]
