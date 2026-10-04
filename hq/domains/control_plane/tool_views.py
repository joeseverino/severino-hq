"""Operator tools: lookups HQ runs on request."""

from __future__ import annotations



from django.shortcuts import redirect
from hq.platform.application.routes import reverse
from django.views.generic import TemplateView

from hq.platform.application.pages import PageMixin
from hq.platform.application.timestamps import moment


class ToolsView(PageMixin, TemplateView):
    """The tools, one tab at a time, answering on a plain GET.

    Deliberately thin. Every tool here is backed by registered capabilities,
    so the generic pages at `/commands/<name>/` already render a form,
    authorize it, execute it and print the result. This page exists because
    those are one tool each, and the question an operator actually has is
    usually several at once.

    So it holds no tool logic: it picks a tab from `application.toolkit`, runs
    that tab's capabilities through the same authorization every other adapter
    uses, and hands the results to the tab's own partial.

    A lookup someone typed is a GET, so a result is a URL. Re-reading a stored
    answer replaces it, so that is a POST, which redirects back to the result.
    """

    template_name = "control_plane/tools.html"
    page_title = "Tools"
    page_lede = "Lookups run from outside this network, not through its rewrites."

    def get_context_data(self, **kwargs):
        from hq.platform.application.capabilities import execute_capability
        from hq.platform.application.security import web_principal
        from hq.platform.application.toolkit import tab_named, tabs_for

        context = super().get_context_data(**kwargs)
        principal = web_principal(self.request.user)
        tabs = tabs_for(principal)
        current = tab_named(self.request.GET.get("tab", ""), principal)
        context["tabs"] = tabs
        context["tab"] = current
        if current is None:
            return context

        # Only what this tab offers, and only what was actually asked. An empty
        # field is not a lookup of the empty string.
        asked = {
            name: self.request.GET.get(name.rpartition(".")[2], "").strip()
            for name in current.capabilities
        }
        context["asked"] = asked
        context["results"] = {
            name: execute_capability(
                name, {name.rpartition(".")[2]: value}, principal=principal
            )
            for name, value in asked.items()
            if value
        }
        # The capability answers with an ISO string, which is what the session
        # and the machine API need. A template wants a datetime, so that HQ's
        # own DATETIME_FORMAT applies rather than a second date style appearing
        # on one page.
        for reading in context["results"].values():
            stamp = reading.get("observed_at") if isinstance(reading, dict) else None
            if (when := moment(stamp, naive="keep")) is not None:
                reading["observed_at"] = when
        return context

    def post(self, request, *args, **kwargs):
        """Re-read an address's stored answer, then show it."""

        from urllib.parse import urlencode

        from hq.platform.application.capabilities import execute_capability
        from hq.platform.application.security import web_principal

        address = request.POST.get("address", "").strip()
        if address:
            execute_capability(
                "lookup.address",
                {"address": address, "refresh": True},
                principal=web_principal(request.user),
            )
        query = urlencode({"tab": request.POST.get("tab", ""), "address": address})
        return redirect(f"{reverse('control_plane:tools')}?{query}")
