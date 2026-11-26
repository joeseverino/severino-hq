"""The controller's own page."""

from typing import override

from django.views.generic import TemplateView

from hq.domains.control_plane.models import InboundSource
from hq.platform.application.connection_setup import docs_url
from hq.platform.application.controller_page import controller_page
from hq.platform.application.entity_links import entity_link
from hq.platform.application.freshness import reading_age
from hq.platform.application.hq_context.sight_part import sight
from hq.platform.application.inbound_events import channel_standing, setup_guide
from hq.platform.application.moments import when
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.tables import Cell, DataTable, TableColumn

PENDING = (
    TableColumn("Record", kind="identifier", cell=lambda work: Cell.of(entity_link("resource", work.resource))),
    TableColumn("Change", drop=2, cell=lambda work: Cell(work.action)),
    TableColumn("State", kind="status", css="key-col", cell=lambda work: Cell(work.state)),
    TableColumn("Asked", kind="status", drop=1, cell=lambda work: Cell(when(work.created_at))),
)


def _result(reading) -> Cell:
    """How a reading ended: its result, or that it failed and what it said."""

    if reading.ok:
        return Cell(reading.result, tone="good")
    return Cell("Failed", tone="serious", lines=(Cell(reading.result, muted=True, cut=True),))


READINGS = (
    TableColumn("Reading", cell=lambda reading: Cell(reading.label, title=reading.kind)),
    TableColumn("Last tried", kind="status", drop=3, cell=lambda reading: Cell(moment=reading.attempted_at)),
    TableColumn(
        "Reading",
        kind="status",
        drop=2,
        cell=lambda reading: Cell(piece=reading_age(reading.reading)),
    ),
    TableColumn("Items", kind="numeric", drop=1, cell=lambda reading: Cell(str(reading.records))),
    TableColumn("Result", kind="status", css="key-col", cell=_result),
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
        return (
            PageAction("Events", reverse("control_plane:events")),
            PageAction("Connections", reverse("control_plane:connections")),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        controller = context["controller"] = controller_page()
        # How current the readings are, counted as HQ's own page counts them.
        context["sight"] = sight()
        listener = context["listener"] = channel_standing(InboundSource.WORKER)
        context["listener_guide"] = setup_guide(listener)
        context["pending"] = DataTable.of("Pending changes", PENDING, controller.queue)
        context["readings"] = DataTable.of("Readings", READINGS, controller.readings)
        context["start_guide"] = docs_url("Install.md", "11-the-controllers-first-reading")
        return context
