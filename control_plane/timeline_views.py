"""One timeline across what HQ did and what its readings saw change."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from application.pages import PageAction, PageMixin
from application.projection import projection_scope
from application.timeline import moments

# The windows offered, in days; the first is the default.
WINDOWS = (1, 7, 30)


class TimelineView(PageMixin, LoginRequiredMixin, TemplateView):
    """Audit, deploys, container starts and reading changes, newest first."""

    template_name = "control_plane/timeline.html"
    page_title = "Timeline"

    def get_page_actions(self):
        return (PageAction("Findings", reverse("control_plane:findings")),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        requested = self.request.GET.get("days", "")
        days = int(requested) if requested.isdigit() and int(requested) in WINDOWS else WINDOWS[1]
        with projection_scope():
            found = moments(since=timezone.now() - timedelta(days=days))
        context.update(
            timeline_rows=[item.row for item in found],
            timeline_days=days,
            timeline_windows=WINDOWS,
        )
        return context
