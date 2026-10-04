"""The admin site, run under the one CSP directive its own JavaScript cannot meet."""

from django.conf import settings
from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig
from django.views.decorators.csp import csp_override


class HQAdminSite(AdminSite):
    """Every admin view, under ``SEVERINO_ADMIN_CSP``.

    The application policy requires Trusted Types, which makes assigning a
    string to ``innerHTML`` throw rather than parse. HQ's own scripts never do
    that; admin's bundled jQuery does, on every page it renders. So the
    relaxation is scoped to that surface rather than weakening the policy
    everywhere.

    ``admin_view`` is the hook Django routes every admin view through, the
    generated per-model ones included, so none can miss it.
    """

    def admin_view(self, view, cacheable=False):
        return csp_override(settings.SEVERINO_ADMIN_CSP)(super().admin_view(view, cacheable))


class HQAdminConfig(AdminConfig):
    """``django.contrib.admin``, with ``HQAdminSite`` as ``admin.site``."""

    default_site = "core.admin_site.HQAdminSite"
