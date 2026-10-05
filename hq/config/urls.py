"""Root URL configuration for Severino HQ."""

from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import URLPattern, include, path

from django.conf import settings

from hq.domains.projects.views import PostureView, WatchingRefreshView, WatchingView
from hq.platform.core.views import (
    AvatarView,
    ConnectionView,
    AgentAccessView,
    ThemeView,
    AgentPolicyView,
    DemoModeView,
    PublicAddressView,
    SearchView,
    ThrottledLoginView,
)
from hq.platform.core.csp_views import csp_report
from hq.platform.core.health_views import health_live, health_ready
from hq.platform.core.action_item_views import (
    ActionItemCountView,
    ActionItemAsideView,
    ActionItemsView,
)
from hq.platform.core.dashboard_views import (
    DashboardLinkChoiceView,
    DashboardGlanceView,
    DashboardGlanceSettingsView,
    DashboardView,
)
from hq.platform.core.command_views import CommandView
from hq.platform.application.domains import host_urlpatterns
from hq.platform.application.plugins import plugin_urlpatterns

def public(urlconf: str):
    """``include(urlconf)`` with every route in it answering without a session.

    For a mount whose views are someone else's (the SSO handshake) or carry
    their own credential (the bearer-token API), where the exemption is a fact
    about the whole mount rather than one view.
    """

    included = include(urlconf)
    for pattern in included[0].urlpatterns:
        if not isinstance(pattern, URLPattern):
            raise TypeError(f"{urlconf} nests an include; mark its views instead.")
        login_not_required(pattern.callback)
    return included


urlpatterns = [
    path("health/live/", health_live, name="health_live"),
    path("health/ready/", health_ready, name="health_ready"),
    # Where the browser reports a policy it refused to follow. The path is in
    # the policy itself, so it is taken from the same setting the policy is
    # built from rather than written twice.
    path(
        settings.SEVERINO_CSP_REPORT_PATH.lstrip("/"),
        csp_report,
        name="csp_report",
    ),
    path(
        "accounts/login/",
        ThrottledLoginView.as_view(),
        name="login",
    ),
    path(
        "accounts/logout/",
        login_not_required(auth_views.LogoutView.as_view()),
        name="logout",
    ),
    path("oidc/", public("mozilla_django_oidc.urls")),
    path("", DashboardView.as_view(), name="dashboard"),
    path("action-items/", ActionItemsView.as_view(), name="action_items"),
    path(
        "action-items/count/", ActionItemCountView.as_view(), name="action_item_count"
    ),
    path(
        "action-items/set-aside/",
        ActionItemAsideView.as_view(aside=True),
        name="action_items_set_aside",
    ),
    path(
        "action-items/bring-back/",
        ActionItemAsideView.as_view(aside=False),
        name="action_items_bring_back",
    ),
    path("demo/", DemoModeView.as_view(), name="demo_mode"),
    path("agent-access/", AgentAccessView.as_view(), name="agent_access"),
    path("theme/", ThemeView.as_view(), name="theme"),
    path("avatar/<str:digest>/", AvatarView.as_view(), name="avatar"),
    path("agents/", AgentPolicyView.as_view(), name="agent_policy"),
    path("connection/", ConnectionView.as_view(), name="connection"),
    path(
        "connection/address/",
        PublicAddressView.as_view(),
        name="tool_public_address",
    ),
    path(
        "dashboard/links/",
        DashboardLinkChoiceView.as_view(),
        name="dashboard_links",
    ),
    path(
        "dashboard/glance/",
        DashboardGlanceView.as_view(),
        name="dashboard_glance",
    ),
    path(
        "dashboard/glance/settings/",
        DashboardGlanceSettingsView.as_view(),
        name="dashboard_glance_settings",
    ),
    path("search/", SearchView.as_view(), name="search"),
    path("commands/<str:name>/", CommandView.as_view(), name="command"),
    path("watching/", WatchingView.as_view(), name="watching"),
    path("watching/refresh/", WatchingRefreshView.as_view(), name="watching_refresh"),
    path("posture/", PostureView.as_view(), name="posture"),
    path("api/", public("hq.platform.api.urls")),
    # Every host domain's own URL configuration, where its declaration mounts it.
    *host_urlpatterns(),
]

urlpatterns.extend(plugin_urlpatterns())

# Development only; see config/devtools.py. Imported here, behind the setting,
# because the package is absent from the host image.
if settings.SEVERINO_DEBUG_TOOLBAR:
    from debug_toolbar.toolbar import debug_toolbar_urls

    urlpatterns.extend(debug_toolbar_urls())

handler500 = "hq.platform.core.error_views.server_error"
