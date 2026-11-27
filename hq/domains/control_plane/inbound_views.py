"""The events page: what HQ was told happened outside it, whether each
channel that tells it is listening, the Worker that hears providers as
Cloudflare runs it, and every event it takes."""

from typing import override

from django.template.loader import render_to_string
from django.views.generic import TemplateView

from hq.domains.control_plane.models import InboundEvent, InboundSource
from hq.platform.application.inbound_events import channel_standing, events_worker, setup_guide, taken
from hq.platform.application.inbound_reads import INBOUND_EVENTS
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.pages import PageAction
from hq.platform.application.problems import About, problems_of
from hq.platform.application.routes import reverse
from hq.platform.application.security import Capability, web_principal
from hq.platform.application.tables import DataTable

from .inbound_tables import EVENT_COLUMNS, grouped, taken_table


class InboundEventListView(ListPageMixin, TemplateView):
    """The ``inbound_events`` list read, newest first under the channel each
    came by, with each channel's standing in the head. Read-only: an event is
    written only as it arrives (``application.inbound``)."""

    required_capability = Capability.READ
    resource = INBOUND_EVENTS
    # The rows are the read's; a stored event is what a derived page fills it with.
    model = InboundEvent
    template_name = "control_plane/event_list.html"
    page_title = "Events"
    page_size = 50
    table_empty = "No events yet."
    table_empty_remedy = False
    table_columns = EVENT_COLUMNS

    @override
    def get_page_actions(self):
        return (PageAction("Controller", reverse("control_plane:controller")),)

    @override
    def drawn(self, label, table, rows) -> DataTable:
        return grouped(super().drawn(label, table, rows), rows)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        standings = [(source.label, channel_standing(source)) for source in InboundSource]
        channels = [(label, standing, setup_guide(standing)) for label, standing in standings]
        context["page_facts"] = render_to_string("control_plane/_inbound_summary.html", {"channels": channels})
        context["taken"] = taken_table(taken())
        context["worker"] = events_worker()
        # About no record with a page: each problem whose fix is read here.
        here = About(under=(reverse("control_plane:events"),))
        context["problems"] = problems_of(here, web_principal(self.request.user))
        return context
