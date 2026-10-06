"""The controller's own page."""

from __future__ import annotations

from hq.platform.application.routes import reverse
from django.views.generic import TemplateView

from hq.platform.application.controller_page import controller_page
from hq.platform.application.pages import PageAction, PageMixin


class ControllerView(PageMixin, TemplateView):
    """Whether the controller is arriving, what it last read, and what waits for it.

    Read-only. The controller is not something HQ operates: it arrives, pulls
    its work and reports, and this page is what HQ knows of that.
    """

    template_name = "control_plane/controller.html"
    page_title = "Controller"
    page_lede = (
        "The controller runs on your server. It reads what HQ shows from each connected "
        "service and applies the changes you queue."
    )

    def get_page_actions(self):
        return (PageAction("Connections", reverse("control_plane:connections")),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["controller"] = controller_page()
        return context
