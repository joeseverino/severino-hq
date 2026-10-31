"""Operator tools: lookups HQ runs on request."""

from typing import override
lazy from urllib.parse import urlencode

from django.shortcuts import redirect
from django.views.generic import TemplateView

from hq.platform.application.pages import PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.timestamps import moment
lazy from hq.platform.application import capabilities, security
lazy from hq.platform.application.toolkit import tab_named, tabs_for


class ToolsView(PageMixin, TemplateView):
    """The tools, one tab at a time, answering on a plain GET.

    Every tool is a registered capability with its own page at
    `/commands/<name>/`; this one exists because an operator's question is usually
    several at once. It holds no tool logic: it runs a tab of `application.toolkit`
    through the shared authorization and hands the results to the tab's partial.
    """

    template_name = "control_plane/tools.html"
    page_title = "Tools"
    page_lede = "These look a name up on the public internet, not in AdGuard."

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        principal = security.web_principal(self.request.user)
        tabs = tabs_for(principal)
        current = tab_named(self.request.GET.get("tab", ""), principal)
        context["tabs"] = tabs
        context["tab"] = current
        if current is None:
            return context

        # Only what this tab offers, and only what was actually asked. An empty
        # field is not a lookup of the empty string.
        asked = {name: self.request.GET.get(name.rpartition(".")[2], "").strip() for name in current.capabilities}
        context["asked"] = asked
        context["results"] = {
            name: capabilities.execute_capability(name, {name.rpartition(".")[2]: value}, principal=principal)
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
        """Re-read an address's stored answer, then show it.

        Replacing a stored answer is a POST, which redirects back to the result, so a
        typed lookup stays a GET whose result is a URL.
        """

        address = request.POST.get("address", "").strip()
        if address:
            capabilities.execute_capability(
                "lookup.address",
                {"address": address, "refresh": True},
                principal=security.web_principal(request.user),
            )
        query = urlencode({"tab": request.POST.get("tab", ""), "address": address})
        return redirect(f"{reverse('control_plane:tools')}?{query}")
