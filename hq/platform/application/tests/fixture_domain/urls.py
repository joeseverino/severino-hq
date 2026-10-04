from django.urls import path

from . import views

app_name = "widgets"

urlpatterns = [path("", views.widget_list, name="list")]
