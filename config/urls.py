"""Root URL configuration for Severino HQ."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import RedirectView

from core.views import (
    DashboardLinkChoiceView,
    DashboardView,
    SearchView,
    ThrottledLoginView,
    health_live,
    health_ready,
)
from application.plugins import plugin_urlpatterns

urlpatterns = [
    path("health/live/", health_live, name="health_live"),
    path("health/ready/", health_ready, name="health_ready"),
    # Django admin ships its own sign-in form. Routed to the login HQ
    # controls so there is exactly one sign-in path, with one set of rules.
    path(
        "admin/login/",
        RedirectView.as_view(url="/accounts/login/", query_string=True),
        name="admin_login_redirect",
    ),
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        ThrottledLoginView.as_view(),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
    path("oidc/", include("mozilla_django_oidc.urls")),
    path("", DashboardView.as_view(), name="dashboard"),
    path(
        "dashboard/links/",
        DashboardLinkChoiceView.as_view(),
        name="dashboard_links",
    ),
    path("search/", SearchView.as_view(), name="search"),
    path("projects/", include("projects.urls")),
    path("content/", include("content.urls")),
    path("docs/", include("docs_index.urls")),
    path("assets/", include("assets.urls")),
    path("expenses/", include("expenses.urls")),
    path("receipts/", include("receipts.urls")),
    path("reports/", include("reports.urls")),
    path("contacts/", include("contacts.urls")),
    path("domains/", include("control_plane.zone_urls")),
    path("infrastructure/", include("control_plane.urls")),
    path("audit/", include("core.urls")),
    path("api/", include("hq_api.urls")),
    path("jobs/", include("jobs.urls")),
]

urlpatterns.extend(plugin_urlpatterns())
