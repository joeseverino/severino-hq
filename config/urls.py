"""Root URL configuration for Severino HQ."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core.views import DashboardView, SearchView

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="auth/login.html"),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
    path("oidc/", include("mozilla_django_oidc.urls")),
    path("", DashboardView.as_view(), name="dashboard"),
    path("search/", SearchView.as_view(), name="search"),
    path("projects/", include("projects.urls")),
    path("content/", include("content.urls")),
    path("docs/", include("docs_index.urls")),
    path("assets/", include("assets.urls")),
    path("expenses/", include("expenses.urls")),
    path("receipts/", include("receipts.urls")),
    path("reports/", include("reports.urls")),
    path("contacts/", include("contacts.urls")),
    path("audit/", include("core.urls")),
]
