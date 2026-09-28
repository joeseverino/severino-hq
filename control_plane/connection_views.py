"""The connections page and asking the controller to read now."""

from __future__ import annotations

from django.contrib import messages

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from application.action_links import (
    READ_NOW_CAPABILITY,
    read_now_payload,
)
from application.capabilities import execute_capability
from application.connection_context import connections_context
from application.security import safe_next, web_principal
from application.pages import PageMixin


class ConnectionListView(PageMixin, LoginRequiredMixin, TemplateView):
    """What HQ can reach, as the controllers last found it.

    Read-only by construction. Every row here started as a 1Password item, and
    the only way to change one is to change that item, so this page reports
    and never edits, which is what keeps it from becoming a second inventory.
    """

    template_name = "control_plane/connection_list.html"
    page_title = "Connections"
    page_lede = "What HQ connects to and what each connection can do."

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["connections"] = connections_context(
            principal=web_principal(self.request.user), request=self.request
        )
        return context


class ReadNowView(LoginRequiredMixin, View):
    """Ask the controller to read one connection, one kind, or everything now.

    POST only; the subject comes from the form or the action's URL.
    """

    http_method_names = ["post"]

    def post(self, request):
        destination = safe_next(request, fallback=reverse("control_plane:connections"))
        result = execute_capability(
            READ_NOW_CAPABILITY,
            read_now_payload({**request.GET.dict(), **request.POST.dict()}),
            principal=web_principal(request.user),
        )
        if result.get("ok"):
            messages.success(request, result["message"])
        else:
            messages.error(request, result["error"]["message"])
        return redirect(destination)
