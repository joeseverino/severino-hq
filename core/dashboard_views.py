"""The dashboard: its glance panels, their settings, the contacts panel and the links an operator pins."""

from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.views.generic import TemplateView, View

from application import action_items as read_state
from application.outward_links import link_choices, outward_links
from application.dashboard import dashboard_highlights, operating_snapshot
from application.glance import (
    dashboard_configuration,
    dashboard_panels,
    glance_context,
    request_dashboard_refresh,
    request_stale_panel_refresh,
    save_dashboard_settings,
)
from application.security import safe_next, web_principal
from application.pages import page_context
from application.ui import ListRow, moment
from contacts import inbox


class DashboardLinkChoiceView(LoginRequiredMixin, View):
    """Choose which outward links the dashboard shows, for this operator only.

    A preference, so it is stored the same way starring a domain is and reaches
    no spec, no generation and no controller: the world does not change because
    somebody decided which shortcuts they want.
    """

    def get(self, request):
        return render(
            request,
            "core/dashboard_links.html",
            {
                "external_choices": link_choices(request.user),
                **page_context(
                    "Dashboard links",
                    "Pick the links to show. With none picked, every one HQ can reach is shown.",
                ),
            },
        )

    def post(self, request):
        from application.pins import DASHBOARD_LINK, replace

        offered = {item["href"].lower() for item in link_choices(None)}
        keep = {
            href.lower()
            for href in request.POST.getlist("href")
            # Only what was offered. A key arriving in a form post is a request,
            # and an unchecked one would let anything be stored as a shortcut.
            if href.lower() in offered
        }
        replace(request.user, DASHBOARD_LINK, keep)
        # One answer for both callers. A browser follows this and lands on the
        # dashboard; a fetch follows it too and reads the panel out of the page
        # it gets back, so what is shown is what was stored rather than what the
        # browser believes was stored.
        return redirect(safe_next(request) or reverse("dashboard"))


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        snapshot = operating_snapshot(principal=web_principal(self.request.user))
        highlights = dashboard_highlights()
        glance = glance_context()
        for project in snapshot["active_projects"]:
            project["updated_at"] = moment(project["updated_at"])
        for collection in (snapshot["draft_content"], snapshot["recent_published"]):
            for item in collection:
                item["updated_at"] = moment(item["updated_at"])
                if item["published_at"]:
                    item["published_at"] = date.fromisoformat(item["published_at"])
        # Unread only, as everywhere else the count is shown.
        action_queue_count = sum(
            item["count"]
            for item in read_state.with_read_state(snapshot["priority"], self.request.user)
            if not item["read"]
        )
        hour = timezone.localtime().hour
        greeting = (
            "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        )
        if name := self.request.user.first_name:
            greeting = f"{greeting}, {name}"

        # Consoles come from the connections a controller reported. Anything
        # else an operator wants here is a fact about their installation and is
        # named in their environment: an address written into this file is
        # published to everyone who clones it and true for nobody else.
        external_links, _ = outward_links(self.request.user)
        external_choices = link_choices(self.request.user)
        # Projected here, not in operating_snapshot(): that snapshot is also the
        # MCP payload, and a transport contract must not carry a UI shape.
        content_rows = [
            ListRow(
                title=item["title"],
                meta=f"{item['content_type_label']} · "
                f"{formats.date_format(item['updated_at'], 'M j')}",
                url=reverse("content:detail", args=[item["slug"]]),
            )
            for item in snapshot["draft_content"]
        ]
        published_rows = [
            ListRow(
                title=item["title"],
                meta=formats.date_format(
                    item["published_at"] or item["updated_at"], "M j"
                ),
                url=item["published_url"]
                or reverse("content:detail", args=[item["slug"]]),
                external=bool(item["published_url"]),
            )
            for item in snapshot["recent_published"]
        ]

        ctx.update(
            greeting=greeting,
            content_rows=content_rows,
            published_rows=published_rows,
            active_project_count=snapshot["kpis"]["active_projects"],
            active_projects=snapshot["active_projects"],
            external_links=external_links,
            external_choices=external_choices,
            draft_content_count=snapshot["kpis"]["draft_content"],
            action_queue_count=action_queue_count,
            profile_action_count=action_queue_count,
            show_action_count=True,
            dashboard_cards=highlights["compact"],
            dashboard_highlights=highlights["highlights"],
            **glance,
        )
        return ctx


class DashboardGlanceView(LoginRequiredMixin, View):
    template_name = "core/_dashboard_glance.html"

    def get(self, request):
        return render(request, self.template_name, glance_context())

    def post(self, request):
        """Request a refresh: of every panel, or with ``scope=stale`` of stale ones.

        The page posts the stale request when it opens on a stale reading; the
        button posts the full one.
        """

        principal = web_principal(request.user)
        if request.POST.get("scope") != "stale":
            request_dashboard_refresh(principal=principal)
            return render(request, self.template_name, glance_context(), status=202)
        configuration = dashboard_configuration()
        panels = dashboard_panels(configuration)
        requested = request_stale_panel_refresh(panels, principal=principal)
        if requested:
            panels = tuple(
                {**panel, "refreshing": True} if panel["id"] in requested else panel
                for panel in panels
            )
        return render(
            request,
            self.template_name,
            glance_context(configuration, panels),
            status=202 if requested else 200,
        )


class DashboardGlanceSettingsView(LoginRequiredMixin, View):
    def get(self, request):
        # The settings are a panel on the dashboard; this address only saves them.
        return redirect("dashboard")

    def post(self, request):
        try:
            save_dashboard_settings(
                weather_point=request.POST.get("weather_point", ""),
                weather_label=request.POST.get("weather_label", ""),
                infrastructure_label=request.POST.get("infrastructure_label", ""),
                principal=web_principal(request.user),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Dashboard settings saved.")
        return redirect("dashboard")


class DashboardContactsView(LoginRequiredMixin, View):
    """Recent submissions, fetched by the dashboard after it has rendered."""

    def get(self, request):
        # The stored rows, kept by the refresh_contacts_inbox timer.
        submissions = inbox.recent()
        rows = [
            ListRow(
                title=submission["name"],
                detail=submission["status"],
                meta=submission["created_at"],
                url=reverse("contacts:detail", args=[submission["id"]]),
            )
            for submission in submissions
        ]
        return render(
            request,
            "core/_dashboard_contacts.html",
            {"recent_contacts": rows, "unread_contacts_count": inbox.unread()[0]},
        )
