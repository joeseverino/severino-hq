from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("", views.AnalyticsOverviewView.as_view(), name="overview"),
]
