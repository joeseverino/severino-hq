"""Root URL configuration for Severino HQ."""

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import URLPattern, include, path

# Development only (config/devtools.py): the image has no toolbar, so the name
# is bound lazily and imported only when the setting below calls it.
lazy from debug_toolbar.toolbar import debug_toolbar_urls

from hq.domains.projects.views import PostureView, WatchingRefreshView, WatchingView
from hq.platform.application.domains import HOST_DOMAINS
from hq.platform.application.plugins import installed_plugins
from hq.platform.core.action_item_views import (
    ActionItemAsideView,
    ActionItemCountView,
    ActionItemsView,
)
from hq.platform.core.command_views import CommandView
from hq.platform.core.csp_views import csp_report
from hq.platform.core.dashboard_views import (
    DashboardGlanceSettingsView,
    DashboardGlanceView,
    DashboardLinkChoiceView,
    DashboardView,
)
from hq.platform.core.health_views import health_live, health_ready
from hq.platform.core.oidc_logout import backchannel_logout
from hq.platform.core.speculation import rules_document
from hq.platform.core.static import root_icon
from hq.platform.core.views import (
    AgentAccessView,
    AgentBlockView,
    AgentPolicyView,
    AvatarView,
    ConnectionPageView,
    DemoModeView,
    HQView,
    OwnDeviceView,
    PublicAddressView,
    SearchView,
    ThemeView,
    ThrottledLoginView,
)


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
        settings.LOGIN_URL.lstrip("/"),
        ThrottledLoginView.as_view(),
        name="login",
    ),
    path(
        "accounts/logout/",
        login_not_required(auth_views.LogoutView.as_view()),
        name="logout",
    ),
    path("oidc/", public("mozilla_django_oidc.urls")),
    # Beside the library's handshake, outside its urlconf: the provider's own server posts here.
    path("oidc/backchannel-logout/", backchannel_logout, name="oidc_backchannel_logout"),
    path("speculation-rules.json", rules_document, name="speculation_rules"),
    path("favicon.ico", root_icon("img/favicon.ico")),
    path("apple-touch-icon.png", root_icon("img/apple-touch-icon.png")),
    path("apple-touch-icon-precomposed.png", root_icon("img/apple-touch-icon.png")),
    path("", DashboardView.as_view(), name="dashboard"),
    path("action-items/", ActionItemsView.as_view(), name="action_items"),
    path("action-items/count/", ActionItemCountView.as_view(), name="action_item_count"),
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
    path("agents/block/", AgentBlockView.as_view(), name="agent_block"),
    path("agents/devices/", OwnDeviceView.as_view(), name="own_device"),
    path("connection/", ConnectionPageView.as_view(), name="connection"),
    path("hq/", HQView.as_view(), name="hq_self"),
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
    *(path(mount.prefix, include(mount.urlconf)) for descriptor in HOST_DOMAINS for mount in descriptor.mounts),
    # Each installed extension's, at the prefix its manifest declares.
    *(path(plugin.url_prefix, include(plugin.urlconf)) for plugin in installed_plugins() if plugin.urlconf),
]

if settings.SEVERINO_DEBUG_TOOLBAR:
    urlpatterns.extend(debug_toolbar_urls())

handler500 = "hq.platform.core.error_views.server_error"
