from django.urls import path

from . import views

app_name = "calendar"

urlpatterns = [
    path("", views.CalendarView.as_view(), name="month"),
    path("sources/<str:source_id>/", views.CalendarSourceView.as_view(), name="source"),
    path("entries/new/", views.EntryCreateView.as_view(), name="entry_new"),
    path("entries/<uuid:uid>/", views.EntryDetailView.as_view(), name="entry"),
    path("entries/<uuid:uid>/edit/", views.EntryUpdateView.as_view(), name="entry_edit"),
    path("entries/<uuid:uid>/delete/", views.EntryDeleteView.as_view(), name="entry_delete"),
]
