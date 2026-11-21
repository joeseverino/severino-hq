"""Operator tools: lookups HQ runs on request."""

from typing import override

from django import forms
from django.shortcuts import redirect
from django.views.generic import TemplateView

from hq.platform.application.moments import moment
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.web_refusals import refused
lazy from hq.platform.application import security
lazy from hq.platform.application.capabilities import command_spec
lazy from hq.platform.application.command_forms import command_form_class
lazy from hq.platform.application.lookup import LOOK_UP_ADDRESS, LOOK_UP_NAME, answers_table
lazy from hq.platform.application.toolkit import tab_named, tabs_for
lazy from hq.platform.application.url_query import with_query


class ToolsView(PageMixin, TemplateView):
    """The tools, one tab at a time, answering on a plain GET.

    Every tool is a registered capability with its own page at
    `/commands/<name>/`; this one exists because an operator's question is usually
    several at once. It holds no tool logic: it runs a tab of `application.toolkit`
    through the shared authorization and hands the results to the tab's partial.
    """

    template_name = "control_plane/tools.html"
    page_title = "Tools"
    page_lede = "Answers from the public internet, not AdGuard."
    # A stored answer is read again by a post of its address here.
    runs = (LOOK_UP_ADDRESS,)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        principal = security.web_principal(self.request.user)
        tabs = tabs_for(principal)
        current = tab_named(self.request.GET.get("tab", ""), principal)
        context["tabs"] = [{"id": each.id, "label": each.label, "current": each == current} for each in tabs]
        context["tab"] = current
        if current is None:
            return context

        # Only what this tab offers, and only what was actually asked. An empty
        # field is not a lookup of the empty string.
        asked = {name: self.request.GET.get(field, "").strip() for name, field in current.capabilities.items()}
        context["lookups"] = {
            name: self._lookup(name, field, current.id, asked[name]) for name, field in current.capabilities.items()
        }
        context["results"] = {
            name: execute_posted(self.request, name, {current.capabilities[name]: value})
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
        name = context["results"].get(LOOK_UP_NAME)
        if isinstance(name, dict) and name.get("ok"):
            context["answers"] = answers_table(name["answers"])
        # A stored answer is read again by a POST of its own, beside the moment it was read.
        address = context["results"].get(LOOK_UP_ADDRESS)
        if isinstance(address, dict) and address.get("observed_at"):
            context["reread"] = PageAction.command(
                LOOK_UP_ADDRESS, fields={"tab": current.id, "address": asked[LOOK_UP_ADDRESS]}
            )
        return context

    @staticmethod
    def _lookup(name: str, field: str, tab: str, asked: str) -> forms.Form:
        """The one field a lookup asks, on the tab it answers on. Both lookups
        stand on one page, so each names its own ids."""

        form = command_form_class(command_spec(name), fields=(field,))(
            initial={field: asked}, auto_id=f"{field}-lookup-%s"
        )
        form.fields["tab"] = forms.CharField(widget=forms.HiddenInput, initial=tab)
        return form

    def post(self, request, *args, **kwargs):
        """Re-read an address's stored answer, then show it.

        Replacing a stored answer is a POST, which redirects back to the result, so a
        typed lookup stays a GET whose result is a URL.
        """

        address = request.POST.get("address", "").strip()
        if address:
            refused(request, execute_posted(request, LOOK_UP_ADDRESS, {"address": address, "refresh": True}))
        return redirect(
            with_query(reverse("control_plane:tools"), {"tab": request.POST.get("tab", ""), "address": address})
        )
