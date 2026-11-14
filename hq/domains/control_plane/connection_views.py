"""The connections page and asking the controller to read now."""

from functools import cached_property
from typing import override

from django.views import View
from django.views.generic import TemplateView
lazy from django.template.loader import render_to_string

from hq.platform.application.action_links import READ_NOW_CAPABILITY, read_now_payload
from hq.platform.application.connection_context import connections_context
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
lazy from hq.platform.application.asks import Ask, AskMixin, Standing, Took, WatchView, read_standing, read_status_url
lazy from hq.platform.application.cadence import forced_kinds


class ConnectionListView(PageMixin, TemplateView):
    """What HQ can reach, as the controllers last found it.

    Read-only by construction. Every row here started as a 1Password item, and
    the only way to change one is to change that item, so this page reports
    and never edits, which is what keeps it from becoming a second inventory.
    """

    template_name = "control_plane/connection_list.html"
    page_title = "Connections"

    @cached_property
    def _connections(self):
        return connections_context(principal=web_principal(self.request.user), request=self.request)

    @override
    def get_page_actions(self):
        # Beside the title with every other page's controls, not on a line of
        # their own halfway down the summary.
        inspect = reverse("connection")
        found = self._connections
        actions = []
        if found.setups or found.unconfigured or found.again:
            # What the page is for when growing HQ: always reachable, whether
            # anything is left unconnected or not.
            actions.append(
                PageAction(
                    "Add a connection", reverse("control_plane:connections") + "#connection-setup-heading", primary=True
                )
            )
        actions.append(PageAction("Your connection", inspect, modal="connection"))
        read_all = found.read_all
        if read_all:
            actions.append(Ask(read_all.label, read_all.url, title=read_all.reason))
        return tuple(actions)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["connections"] = self._connections
        context["page_facts"] = render_to_string("control_plane/_connection_summary.html", context)
        return context


class ReadNowView(AskMixin, View):
    """Ask the controller to read one connection, one kind, or everything now,
    named by the form or the action's URL."""

    capability = READ_NOW_CAPABILITY
    back_to = "control_plane:connections"

    @override
    def payload(self):
        return read_now_payload({**self.request.GET.dict(), **self.request.POST.dict()})

    @override
    def asked(self, result, at) -> Took:
        said = str(result.get("message") or "")
        if not result.get("read_now"):
            # A bare wake-up names nothing to wait for.
            return Took(Standing(note=said))
        payload = self.payload()
        ref = str(payload.get("connection_ref", ""))
        kinds = forced_kinds(ref, str(payload.get("kind", ""))) or ()
        return Took(
            read_standing(kinds, at, connection_ref=ref),
            read_status_url(kinds, at, connection_ref=ref),
            said,
        )


class ReadStatusView(WatchView):
    """How a read the controller was asked for stands: it asks nothing of
    anyone, and reads when each kind was last stored."""
