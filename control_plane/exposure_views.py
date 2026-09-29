"""What the internet can reach, and the problems ranked by it."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.views.generic import TemplateView

from application.dashboard import queue_item
from application.exposure_board import exposure_board
from application.pages import PageAction, PageMixin
from application.projection import projection_scope


class ExposureView(PageMixin, LoginRequiredMixin, TemplateView):
    """Every name by who can reach it; every container problem by how bad that is."""

    template_name = "control_plane/exposure.html"
    page_title = "Exposure"

    def get_page_actions(self):
        return (
            PageAction("Findings", reverse("control_plane:findings")),
            PageAction("Containers", reverse("control_plane:containers")),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        with projection_scope():
            board = exposure_board()
        context.update(
            {
                "exposure_counts": board.counts,
                "exposure_problems": [
                    queue_item("hq.containers", "Containers", item) for item in board.problems
                ],
                "sections": board.sections,
            }
        )
        return context
