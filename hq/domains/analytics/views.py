"""The analytics page. A delivery adapter and nothing else.

Every number here is computed in ``application.analytics``; this chooses the
window, asks once, and renders. Nothing is joined or summed in a template.
"""

from django.views.generic import TemplateView

from hq.platform.application.analytics import DEFAULT_WINDOW_DAYS, overview
from hq.platform.application.pages import PageMixin


class AnalyticsOverviewView(PageMixin, TemplateView):
    template_name = "analytics/overview.html"
    page_title = "Analytics"

    def get_context_data(self, **kwargs):
        try:
            days = int(self.request.GET.get("days", DEFAULT_WINDOW_DAYS))
        except (TypeError, ValueError):
            # An unreadable window is the default window, not an error page.
            # The value comes from a link, and a mistyped one should still show
            # the operator their traffic.
            days = DEFAULT_WINDOW_DAYS
        context = super().get_context_data(**kwargs) | overview(days=days)
        # Page speed is three rates or, with no visit measured, one sentence.
        context["vitals_measured"] = any(
            metric["percent"] is not None for metric in context.get("vitals", {}).values()
        )
        return context
