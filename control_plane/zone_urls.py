"""Web → Domains.

Mounted apart from the infrastructure registry because it answers a different
question for a different reason, and an operator looking for what a domain
publishes should not have to know it is stored as a control-plane resource.
"""

from django.urls import path

from . import zone_views

app_name = "zones"

urlpatterns = [
    path("", zone_views.ZoneIndexView.as_view(), name="index"),
    # <str:> rather than <slug:>, because a domain has dots in it and a slug
    # does not -- the same reason the service routes use it.
    path("<str:zone>/", zone_views.ZoneDetailView.as_view(), name="detail"),
    path("<str:zone>/adopt/", zone_views.ZoneAdoptView.as_view(), name="adopt"),
    path("<str:zone>/pin/", zone_views.ZonePinView.as_view(), name="pin"),
    path("<str:zone>/mail/", zone_views.ZoneMailView.as_view(), name="mail"),
    # No route for adopting records. Taking on a domain takes on what is in it,
    # and every sweep takes on whatever appeared since -- so there is no moment
    # at which an operator needs to ask for it. Two views and two URLs existed
    # for a button that turned out to be a question with one answer.
]
