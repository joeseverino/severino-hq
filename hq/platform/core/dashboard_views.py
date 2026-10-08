"""The dashboard: its glance panels, their settings, the contacts panel and the links an operator pins."""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, ClassVar, override

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from hq.domains.contacts import inbox
from hq.platform.application import action_items as queue_state, fragments
from hq.platform.application.cadence import controller_standing
from hq.platform.application.calendar import calendar_month, month_of
from hq.platform.application.calendar_entries import calendar_choices
from hq.platform.application.dashboard import dashboard_highlights, operating_snapshot
from hq.platform.application.derivations import every_revision
from hq.platform.application.glance import (
    dashboard_configuration,
    dashboard_panels,
    glance_context,
    request_dashboard_refresh,
    request_stale_panel_refresh,
    save_dashboard_settings,
)
from hq.platform.application.moments import when_day
from hq.platform.application.outward_links import link_choices, outward_links
from hq.platform.application.pages import page_context
from hq.platform.application.request_path import request_path
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.timestamps import moment
from hq.platform.application.ui import ListRow

# A queue of this many or fewer is read on the dashboard itself, each card
# with its button; a longer one is a count and a link to its page.
NEEDS_YOU_IN_PLACE = 3


class DashboardLinkChoiceView(View):
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
                    "Tick the links to show. With none ticked, all of them show.",
                ),
            },
        )

    def post(self, request):
        from hq.platform.application.pins import DASHBOARD_LINK, replace

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


class DashboardView(fragments.FragmentMixin, TemplateView):
    template_name = "dashboard.html"

    # A part of the dashboard asked for by name is composed alone: paging the
    # month or saving the links reads what that card shows and nothing else.
    def _calendar(self) -> dict:
        today = timezone.localdate()
        month = month_of(self.request.GET.get("month"), today)
        return {
            "calendar": calendar_month(
                month, choices=calendar_choices(self.request.user), today=today
            ),
            "calendar_paging": {
                "previous": f"?month={(month - timedelta(days=1)):%Y-%m}",
                "next": f"?month={(month + timedelta(days=32)):%Y-%m}",
                "today": "?",
            },
        }

    def _links(self) -> dict:
        # Consoles come from the connections a controller reported. Anything
        # else an operator wants here is a fact about their installation and is
        # named in their environment: an address written into this file is
        # published to everyone who clones it and true for nobody else.
        external_links, _ = outward_links(self.request.user)
        return {
            "external_links": external_links,
            "external_choices": link_choices(self.request.user),
        }

    PARTS: ClassVar[dict[str, Callable[..., Any]]] = {"calendar": _calendar, "links": _links}

    @override
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        part = self.PARTS.get(fragments.requested(self.request))
        if part is not None:
            ctx.update(part(self))
            return ctx
        snapshot = operating_snapshot(principal=web_principal(self.request.user))
        highlights = dashboard_highlights()
        glance = glance_context()
        for project in snapshot["active_projects"]:
            project["updated_at"] = moment(project["updated_at"], naive="keep")
        for collection in (snapshot["draft_content"], snapshot["recent_published"]):
            for item in collection:
                item["updated_at"] = moment(item["updated_at"], naive="keep")
                if item["published_at"]:
                    item["published_at"] = date.fromisoformat(item["published_at"])
        # What waits on this person, counted as the header's count endpoint
        # counts it: everything open that they have not set aside.
        queue = queue_state.with_aside_state(snapshot["priority"], self.request.user)
        doing, told = queue_state.split_waiting(queue)
        action_queue_count = len(doing)
        hour = timezone.localtime().hour
        greeting = (
            "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        )
        if name := self.request.user.first_name:
            greeting = f"{greeting}, {name}"

        # Projected here, not in operating_snapshot(): that snapshot is also the
        # MCP payload, and a transport contract must not carry a UI shape.
        content_rows = [
            ListRow(
                title=item["title"],
                meta=f"{item['content_type_label']} · "
                f"{when_day(item['updated_at'])}",
                url=reverse("content:detail", args=[item["slug"]]),
            )
            for item in snapshot["draft_content"]
        ]
        published_rows = [
            ListRow(
                title=item["title"],
                meta=when_day(item["published_at"] or item["updated_at"]),
                url=item["published_url"]
                or reverse("content:detail", args=[item["slug"]]),
                external=bool(item["published_url"]),
            )
            for item in snapshot["recent_published"]
        ]

        ctx.update(
            **self._calendar(),
            **self._links(),
            greeting=greeting,
            # The reading the connection panel opens on, so the header and the
            # panel cannot disagree about a request. Rendered with the page: a
            # line that arrives after it is a line that visibly changes.
            request_connection=request_path(self.request).connection,
            controller=controller_standing(),
            # The stored rows, kept by the scheduled contacts.inbox job: reading
            # them asks no one, so the card is drawn with the page.
            recent_contacts=[
                ListRow(
                    title=submission["name"],
                    detail=submission["status"],
                    meta=submission["created_at"],
                    url=reverse("contacts:detail", args=[submission["id"]]),
                )
                for submission in inbox.recent()
            ],
            unread_contacts_count=inbox.unread()[0],
            content_rows=content_rows,
            published_rows=published_rows,
            active_project_count=snapshot["kpis"]["active_projects"],
            active_projects=snapshot["active_projects"],
            draft_content_count=snapshot["kpis"]["draft_content"],
            action_queue_count=action_queue_count,
            # A queue short enough to read in place is shown in place.
            needs_you=doing if len(doing) <= NEEDS_YOU_IN_PLACE else [],
            notice_count=len(told),
            aside_count=len(queue) - action_queue_count - len(told),
            profile_action_count=action_queue_count + len(told),
            show_action_count=True,
            dashboard_cards=highlights["compact"],
            dashboard_highlights=highlights["highlights"],
            # Each domain's leading chart; its own calendar is read on its pages.
            dashboard_charts=[
                chart
                for section in highlights["highlights"]
                if section["overview"] is not None
                for chart in section["overview"].charts[:1]
            ],
            **glance,
        )
        return ctx


class DashboardGlanceView(View):
    template_name = "core/_dashboard_glance.html"

    def get(self, request):
        """The strip as it stands; "unchanged" to a poll while nothing was written."""

        return fragments.render(
            request, self.template_name, glance_context, revision=every_revision()
        )

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


class DashboardGlanceSettingsView(View):
    def get(self, request):
        return render(
            request,
            "core/dashboard_glance_settings.html",
            {
                **page_context("Dashboard settings", trail=(("Dashboard", reverse("dashboard")),)),
                "settings": dashboard_configuration(),
            },
        )

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
