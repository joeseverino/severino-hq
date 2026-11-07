from django.urls import path

from .views import index, note

app_name = "example_extension"
urlpatterns = [path("", index, name="index"), path("<slug:slug>/", note, name="note")]
