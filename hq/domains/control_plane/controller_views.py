"""The controller's own page."""

from typing import override

from django.views.generic import TemplateView

from hq.platform.application.connection_setup import docs_url
from hq.platform.application.controller_page import controller_page
from hq.platform.application.entity_links import entity_link
from hq.platform.application.moments import ago
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.tables import Cell, DataTable, TableColumn

PENDING = (
    TableColumn("Record", kind="identifier", cell=lambda work: Cell.of(entity_link("resource", work.resource))),
    TableColumn("Change", drop=2, cell=lambda work: Cell(work.action)),
    TableColumn("State", kind="status", css="key-col", cell=lambda work: Cell(work.state)),
    TableColumn("Asked", kind="status", drop=1, cell=lambda work: Cell(ago(work.created_at))),
)


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

    @override
    def get_page_actions(self):
        return (PageAction("Connections", reverse("control_plane:connections")),)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        controller = context["controller"] = controller_page()
        context["pending"] = DataTable.of("Pending changes", PENDING, controller.queue)
        context["start_guide"] = docs_url("Install.md", "11-the-controllers-first-reading")
        return context
