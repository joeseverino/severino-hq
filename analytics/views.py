"""The analytics page. A delivery adapter and nothing else.

Every number here is computed in ``application.analytics``; this chooses the
window, asks once, and renders. Nothing is joined or summed in a template.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from application.analytics import DEFAULT_WINDOW_DAYS, overview


class AnalyticsOverviewView(LoginRequiredMixin, TemplateView):
    template_name = "analytics/overview.html"

    def get_context_data(self, **kwargs):
        try:
            days = int(self.request.GET.get("days", DEFAULT_WINDOW_DAYS))
        except (TypeError, ValueError):
            # An unreadable window is the default window, not an error page.
            # The value comes from a link, and a mistyped one should still show
            # the operator their traffic.
            days = DEFAULT_WINDOW_DAYS
        return super().get_context_data(**kwargs) | overview(days=days)
