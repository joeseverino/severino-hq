"""Dashboard + audit-log views."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView, View

from application import action_items as read_state
from application.agent_access import set_agents_paused
from application.connections import link_choices, outward_links
from application.command_center import command_center
from application.dashboard import dashboard_highlights, operating_snapshot, work_queue
from application.glance import (
    dashboard_configuration,
    dashboard_panels,
    glance_context,
    request_dashboard_refresh,
    request_stale_panel_refresh,
    save_dashboard_settings,
)
from application.projection import projection_scope
from application.plugins import plugin_health
from application.search import global_search
from application.security import AuthorizationError, safe_next, web_principal
from application.pages import PageAction, PageMixin, page_context
from application.tables import TableColumn, TableFilter, TableListMixin
from application.ui import ListRow, counted
from contacts import inbox
from .audit import record_event
from .middleware import DEMO_SESSION_KEY
from .models import AuditLog
from .network import client_ip


class ThrottledLoginView(LoginView):
    """The password form, with a cost attached to guessing at it.

    Refused *before* the credentials are checked. Validating first and
    discarding the result would still answer the attacker's actual question
    (response timing, and the difference between "no such user" and "locked",
    both leak whether a guess was close) and would spend a password hash per
    attempt doing it, which is the expensive operation an attacker wants to
    provoke.

    The message names no account and no address. It says the door is shut and
    when it reopens, which is everything a locked-out operator needs and
    nothing an attacker can use to tell whether they found a real username.
    """

    template_name = "auth/login.html"

    @property
    def sso_only(self) -> bool:
        return (
            settings.SEVERINO_OIDC_ENABLED
            and not settings.SEVERINO_PASSWORD_LOGIN_ENABLED
        )

    def get(self, request, *args, **kwargs):
        """Go straight to Pocket ID rather than asking which door to use.

        Strictly less friction than the button it replaces: signing in is
        already a redirect to the identity provider, and stopping to confirm
        that is a click that decides nothing.

        Except after signing out, where bouncing would immediately return the
        still-valid provider session and make the sign-out look broken. There,
        the page is shown so leaving is possible.
        """

        if self.sso_only and "signed_out" not in request.GET:
            target = reverse("oidc_authentication_init")
            # Checked here even though the provider library checks it again
            # before use. A destination is only carried forward if it points
            # back at this host, so nothing downstream has to be trusted to
            # notice that it does not.
            nxt = safe_next(request)
            if nxt:
                return redirect(f"{target}?next={quote(nxt)}")
            return redirect(target)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        from .network import client_ip
        from .throttle import lockout

        if self.sso_only:
            # There is no backend to check it against, so this could only ever
            # fail, but failing here means it is never carried further, and
            # the attempt is answered the same way whatever was submitted.
            return HttpResponseForbidden("Password sign-in is disabled.")

        state = lockout(request.POST.get("username", ""), client_ip(request))
        if not state.locked:
            return super().post(request, *args, **kwargs)
        form = self.get_form()
        form.errors.pop("__all__", None)
        form.add_error(
            None,
            "Too many failed sign-in attempts. Try again in "
            f"{counted(state.minutes_remaining, 'minute')}.",
        )
        return self.render_to_response(self.get_context_data(form=form), status=429)


def health_live(request):
    """Minimal process liveness probe; never touches an external dependency."""

    return JsonResponse({"status": "ok"})


def health_ready(request):
    """Prove HQ can safely serve traffic without disclosing configuration."""

    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            checks["database"] = cursor.fetchone() == (1,)
        executor = MigrationExecutor(connection)
        checks["migrations"] = not executor.migration_plan(
            executor.loader.graph.leaf_nodes()
        )
    except Exception:  # noqa: BLE001 - readiness must fail closed
        checks["database"] = False
        checks["migrations"] = False

    writable_paths = (
        settings.MEDIA_ROOT,
        settings.EXPORTS_ROOT,
        settings.STATIC_ROOT,
        Path(settings.DATABASES["default"]["NAME"]).parent,
    )
    checks["storage"] = all(
        path.is_dir() and os.access(path, os.W_OK) for path in writable_paths
    )
    # Aggregated for anonymous callers, itemised for signed-in ones.
    #
    # This endpoint answers without a credential, because a container
    # healthcheck cannot sign in. A probe only needs to know whether HQ can
    # serve traffic at all; which extension is unhealthy is an operator's
    # question, and is answered to operators.
    plugins = plugin_health()
    if plugins:
        checks["plugins"] = all(plugins.values())
        if getattr(request.user, "is_authenticated", False):
            checks.update({f"plugin:{key}": value for key, value in plugins.items()})
    ready = all(checks.values())
    return JsonResponse(
        {"status": "ok" if ready else "unavailable", "checks": checks},
        status=200 if ready else 503,
    )


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
            project["updated_at"] = datetime.fromisoformat(project["updated_at"])
        for collection in (snapshot["draft_content"], snapshot["recent_published"]):
            for item in collection:
                item["updated_at"] = datetime.fromisoformat(item["updated_at"])
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


ACTION_ITEM_FILTERS = ("q", "status", "source")


def _action_items(request, current=None):
    """Every action item with its read state, and those the request's filters keep."""

    if current is None:
        with projection_scope():
            current = work_queue()
    all_items = read_state.with_read_state(current, request.user)
    items = read_state.filter_items(
        all_items,
        query=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        source=request.GET.get("source", ""),
    )
    return all_items, items


class ActionItemsView(PageMixin, LoginRequiredMixin, TemplateView):
    """The full human surface for HQ's one composed attention queue."""

    template_name = "action_items.html"
    page_title = "Action items"

    def get_page_actions(self):
        if not self._unread:
            return ()
        # The filters travel in the URL, so "all" means all that are shown.
        shown = self.request.GET.copy()
        for name in list(shown):
            if name not in ACTION_ITEM_FILTERS:
                del shown[name]
        url = reverse("action_items_read_all")
        if shown:
            url = f"{url}?{shown.urlencode()}"
        return (PageAction("Mark all read", url, method="post"),)

    def get_context_data(self, **kwargs):
        all_items, items = _action_items(self.request)
        self._unread = [item for item in items if not item["read"]]
        context = super().get_context_data(**kwargs)
        status = self.request.GET.get("status", "").strip()
        source = self.request.GET.get("source", "").strip()
        sources = tuple(
            {item["source_id"]: item["source"] for item in all_items}.items()
        )
        unread = self._unread
        context.update(
            action_items=unread,
            read_action_items=[item for item in items if item["read"]],
            action_item_total=sum(item["count"] for item in unread),
            profile_action_count=sum(item["count"] for item in all_items if not item["read"]),
            show_action_count=True,
            action_sources=sources,
            action_query=self.request.GET.get("q", "").strip(),
            action_status=status,
            action_source=source,
        )
        return context


class DemoModeView(LoginRequiredMixin, View):
    """Turn substituted values on or off for this browser.

    POST because it changes what every number on every page means, and a thing
    that can be flipped by following a link can be flipped by an image tag on
    another page. Nothing is written beyond the session, so the audit entry is
    the only trace it leaves, and it is recorded because an operator who
    forgets the mode is on can screenshot fiction and file it as fact.
    """

    def post(self, request):
        showing = not request.session.get(DEMO_SESSION_KEY)
        request.session[DEMO_SESSION_KEY] = showing
        record_event(
            action=AuditLog.Action.UPDATED,
            obj=request.user,
            type_label="Demo mode",
            message="Demo mode on" if showing else "Demo mode off",
            user=request.user,
        )
        # No message. The switch shows its own state and the header carries a
        # mark while it is on: a paragraph on every flip is a third telling
        # of something already said twice.
        return redirect(safe_next(request, fallback=reverse("dashboard")))


class AgentAccessView(LoginRequiredMixin, View):
    """Pause or resume every agent. The form sends a state, never a toggle."""

    def post(self, request):
        requested = request.POST.get("paused")
        if requested not in {"0", "1"}:
            return HttpResponseBadRequest("paused must be 0 or 1")
        set_agents_paused(
            requested == "1",
            principal=web_principal(request.user),
            user=request.user,
        )
        return redirect(safe_next(request, fallback=reverse("dashboard")))


class AgentPolicyView(PageMixin, LoginRequiredMixin, TemplateView):
    """Capability policy. Reads from matrix(), writes through apply_changes()."""

    template_name = "core/agent_policy.html"
    page_title = "Agents"

    def get_context_data(self, **kwargs):
        from datetime import timedelta


        from application import capability_policy
        from application.approvals import awaiting_ids

        context = super().get_context_data(**kwargs)
        context["columns"], context["groups"] = capability_policy.matrix()
        context["agents"] = [column for column in context["columns"] if column.identity]
        # A column whose every settable rule is dormant is off as a whole, and
        # says so once in its header rather than greying each cell unexplained.
        # A row's cells are in column order, so a cell's column is its position.
        settable = [
            (index, cell)
            for group in context["groups"]
            for row in group.rows
            for index, cell in enumerate(row.cells)
            if not cell.unavailable
        ]
        context["any_dormant"] = any(cell.dormant for _index, cell in settable)
        context["column_heads"] = [
            {
                "column": column,
                "dormant": any(index == position for index, _cell in settable)
                and all(cell.dormant for index, cell in settable if index == position),
            }
            for position, column in enumerate(context["columns"])
        ]
        context["rule_count"] = len(capability_policy.rules())
        context["awaiting_count"] = len(awaiting_ids())
        context["refused_count"] = AuditLog.objects.filter(
            action=AuditLog.Action.DENIED, created_at__gte=timezone.now() - timedelta(hours=24)
        ).count()
        return context

    def post(self, request):
        from application import capability_policy

        changed, problems = capability_policy.apply_changes(
            request.POST, principal=web_principal(request.user), user=request.user
        )
        for problem in problems:
            messages.error(request, problem)
        if changed:
            messages.success(
                request, f"Saved {counted(changed, 'change')}. Each is in the audit log."
            )
        elif not problems:
            messages.info(request, "Nothing changed.")
        return redirect("agent_policy")


class ActionItemCountView(LoginRequiredMixin, View):
    """The unread count for the header, fetched after the page rather than during it."""

    def get(self, request):
        count = read_state.unread_count(work_queue(), request.user)
        return JsonResponse({"count": count})


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


class ActionItemReadView(LoginRequiredMixin, View):
    """Mark action items read or unread for the signed-in person.

    Each route names the state in ``read``, so a button posts only the item's
    key as its own value.
    """

    read = False

    def post(self, request):
        with projection_scope():
            current = work_queue()
        read_state.mark(
            request.user,
            request.POST.getlist("key"),
            read=self.read,
            current=current,
        )
        return redirect(
            safe_next(request, scope=reverse("action_items"), fallback=reverse("action_items"))
        )


class ActionItemReadAllView(LoginRequiredMixin, View):
    """Mark read every unread item the page's filters (in the query string) show."""

    def post(self, request):
        with projection_scope():
            current = work_queue()
        _, items = _action_items(request, current)
        read_state.mark(
            request.user,
            [item["key"] for item in items if not item["read"]],
            read=True,
            current=current,
        )
        return redirect(
            safe_next(request, scope=reverse("action_items"), fallback=reverse("action_items"))
        )


class SearchView(PageMixin, LoginRequiredMixin, TemplateView):
    template_name = "search.html"
    page_title = "Command Center"
    page_lede = "Search records, resources, connections, and commands."
    result_limit = 8
    palette_search_limit = 3
    palette_search_total_limit = 12
    palette_result_limit = 25
    palette_group_limit = 5
    palette_scope_priority = {
        "infrastructure.resources": 0,
        "projects": 1,
        "content": 2,
        "documentation": 3,
        "assets": 4,
        "expenses": 5,
        "receipts": 6,
        "audit": 100,
    }

    def get_template_names(self):
        if self.request.headers.get("X-Command-Center") == "palette":
            return ["core/_command_center_results.html"]
        return super().get_template_names()

    def _palette_groups(self, discovery):
        remaining = self.palette_result_limit
        groups = []
        for key, label in (
            ("estate", "Estate"),
            ("commands", "Commands"),
            ("views", "Topology views"),
            ("resources", "Resources"),
            ("connections", "Connections"),
            ("checks", "Checks"),
        ):
            items = tuple(item for item in discovery[key] if item.url)[
                : min(self.palette_group_limit, remaining)
            ]
            if items:
                groups.append({"key": key, "label": label, "items": items})
                remaining -= len(items)
            if not remaining:
                break
        return groups

    def _palette_search_groups(self, groups):
        remaining = self.palette_search_total_limit
        visible = []
        ordered = sorted(
            groups,
            key=lambda group: self.palette_scope_priority.get(group["scope"], 20),
        )
        for group in ordered:
            items = tuple(group["items"][:remaining])
            if items:
                visible.append({**group, "items": items})
                remaining -= len(items)
            if not remaining:
                break
        return visible

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()
        groups: list[dict] = []
        contacts: list = []
        total = 0
        principal = web_principal(self.request.user)
        palette_request = self.request.headers.get("X-Command-Center") == "palette"
        # One scope, so records search and discovery share the machine and
        # connection reads they both make.
        with projection_scope():
            if q and (not palette_request or len(q) >= 2):
                outcome = global_search(
                    q,
                    principal=principal,
                    limit_per_scope=(
                        self.palette_search_limit if palette_request else self.result_limit
                    ),
                )
                groups = outcome["groups"]
                total = outcome["total"]
                if not palette_request:
                    contacts = inbox.search(q, limit=self.result_limit)
                    total += len(contacts)
            discovery = command_center(
                q, principal=principal, include_live_connections=True
            )
        palette_groups = self._palette_groups(discovery)
        palette_search_groups = self._palette_search_groups(groups)
        palette_search_count = sum(
            len(group["items"]) for group in palette_search_groups
        )
        discovery_total = sum(len(discovery[key]) for key in discovery)
        ctx.update(
            q=q,
            search_query=q,
            groups=groups,
            contacts=contacts,
            total=total,
            discovered_estate=discovery["estate"],
            discovered_resources=discovery["resources"],
            discovered_commands=discovery["commands"],
            discovered_connections=discovery["connections"],
            discovered_views=discovery["views"],
            discovered_checks=discovery["checks"],
            discovery_total=discovery_total,
            # The estate leads, above records and the audit log.
            palette_estate=[group for group in palette_groups if group["key"] == "estate"],
            palette_groups=[group for group in palette_groups if group["key"] != "estate"],
            palette_search_groups=palette_search_groups,
            palette_count=(
                sum(len(group["items"]) for group in palette_groups)
                + palette_search_count
            ),
            palette_total=discovery_total + total,
            palette_result_limit=self.palette_result_limit,
        )
        return ctx


class AuditLogListView(PageMixin, TableListMixin, LoginRequiredMixin, ListView):
    model = AuditLog
    template_name = "core/auditlog_list.html"
    context_object_name = "events"
    paginate_by = 50
    page_title = "Audit log"
    page_lede = "Every change, sign-in, export and refusal, newest first."
    table_search_scope = "audit"
    table_selectable = True
    table_filters = (
        TableFilter("action", "Action", "action", AuditLog.Action.choices),
    )
    table_columns = (
        TableColumn("When", "created_at", "Oldest event", "Newest event"),
        TableColumn("Who", "user__username", "User A–Z", "User Z–A"),
        TableColumn("Action", "action", "Action", "Action reverse"),
        TableColumn("Object", "object_type", "Object type", "Object type reverse"),
        TableColumn("Message", "message", "Message A–Z", "Message Z–A"),
    )
    table_default_sort = "-created_at"
    table_search_placeholder = "Search objects, operation IDs, and messages…"

    def get_page_actions(self):
        from application.approvals import awaiting_ids

        listing = reverse("core:audit_list")
        awaiting = self._awaiting()
        return (
            PageAction("All events", listing, primary=not awaiting),
            PageAction(
                f"Awaiting approval · {len(awaiting_ids())}",
                f"{listing}?awaiting=1",
                primary=awaiting,
            ),
        )

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user")
        if self._awaiting():
            from application.approvals import AUDIT_LABELS, awaiting_ids

            qs = qs.filter(
                action=AuditLog.Action.CREATED,
                object_type__in=AUDIT_LABELS,
                object_id__in=awaiting_ids(),
            )
        return self.apply_table_query(qs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["awaiting"] = self._awaiting()
        return context

    def _awaiting(self) -> bool:
        """The log narrowed to requests still waiting for a person."""

        return self.request.GET.get("awaiting") == "1"


class AuditLogDetailView(PageMixin, LoginRequiredMixin, DetailView):
    """One event, in full, and what sits either side of it.

    The list can only ever show a line per event. What an audit trail is
    actually consulted for is the question behind the line (which field
    moved, what the value was before, what else the same action touched) and
    none of that fits in a row. So every row leads here.
    """

    model = AuditLog
    template_name = "core/auditlog_detail.html"
    context_object_name = "event"

    def get_queryset(self):
        return AuditLog.objects.select_related("user")

    def held_approval(self):
        """The approval this event opened, reviewed, if it opened one."""

        if not hasattr(self, "_held_approval"):
            from application import approvals

            held = approvals.for_audit_event(self.object)
            self._held_approval = approvals.review(held) if held is not None else None
        return self._held_approval

    def get_page_title(self):
        return "Approval" if self.held_approval() is not None else "Audit event"

    def get_page_trail(self):
        return (("Audit log", reverse("core:audit_list")),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        event = self.object

        # Field-level changes, as rows rather than as a blob of JSON.
        changes = event.metadata.get("changes") or {}
        context["changes"] = [
            {"field": field, "before": pair[0], "after": pair[1]}
            for field, pair in sorted(changes.items())
            if isinstance(pair, list) and len(pair) == 2
        ]
        # Everything else in the metadata, minus what is already rendered.
        context["extra"] = {
            key: value
            for key, value in sorted(event.metadata.items())
            if key != "changes"
        }

        # The rest of the same operation: what `operation_id` is for. One
        # action can touch many rows, and matching them up by timestamp alone
        # is guesswork.
        if event.operation_id:
            context["siblings"] = (
                AuditLog.objects.filter(operation_id=event.operation_id)
                .exclude(pk=event.pk)
                .select_related("user")[:50]
            )
        # Everything else that ever happened to this object.
        if event.object_type and event.object_id:
            context["history"] = (
                AuditLog.objects.filter(
                    object_type=event.object_type, object_id=event.object_id
                )
                .exclude(pk=event.pk)
                .select_related("user")[:20]
            )
        approval = self.held_approval()
        if approval is not None:
            context["approval"] = approval
        return context


class ApprovalEntryView(LoginRequiredMixin, View):
    """A stable link to a held request's audit entry."""

    def get(self, request, approval_id):
        from application.approvals import entry_event

        event = entry_event(approval_id)
        if event is None:
            return redirect(f"{reverse('core:audit_list')}?awaiting=1")
        return redirect("core:audit_detail", pk=event.pk)


class ConnectionView(PageMixin, LoginRequiredMixin, TemplateView):
    """Why this request was allowed to arrive, layer by layer.

    A page rather than only a dialog, for the same reason every other dialog
    here has one behind it: the panel is an enhancement, and the answer has to
    exist for somebody who followed the link with script off, or who wants to
    send it to themselves.
    """

    template_name = "core/connection.html"
    page_title = "This connection"
    page_lede = (
        "Why this request reached HQ, which identities agree, and the evidence "
        "behind every admission decision."
    )

    def get_context_data(self, **kwargs):
        from application.connection import (
            addresses_of,
            addresses_of_hq,
            connection as describe,
            headers_of,
            hops_of,
        )
        from application.connection_security import observed_request_controls

        context = super().get_context_data(**kwargs)
        # Provider observations are cached facts. The request explanation may
        # derive from them, but opening the panel never probes NPM or handles a
        # credential.
        edge, firewall = observed_request_controls(self.request.get_host())
        found = describe(self.request, edge=edge, firewall=firewall)
        context["connection"] = found
        context["addresses"] = addresses_of(found)
        context["hq_addresses"] = addresses_of_hq(found)
        context["hops"] = hops_of(self.request)
        context["headers"] = headers_of(self.request)
        return context


# The browser's own account of a policy it refused to follow. Bounded on every
# axis a stranger controls: how much it may send, how much of that is kept, and
# how often the same complaint may reach the database.
_CSP_REPORT_MAX_BYTES = 8 * 1024
_CSP_FIELD_LIMIT = 200
_CSP_REPEAT_WINDOW_SECONDS = 3600


def _csp_violations(payload):
    """The violations in a report body, whichever of the two shapes it uses.

    `application/csp-report` sends one violation under a `csp-report` key;
    the Reporting API sends a list of report objects with the violation under
    `body`. Both are read, because which one arrives depends on the browser
    and neither is worth losing.
    """

    if isinstance(payload, dict):
        report = payload.get("csp-report")
        return [report] if isinstance(report, dict) else []
    if isinstance(payload, list):
        return [
            item["body"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("body"), dict)
        ]
    return []


@csrf_exempt
@require_POST
def csp_report(request):
    """Record a Content-Security-Policy violation the browser refused to run.

    The policy is the one boundary HQ cannot verify from the inside: it is
    enforced in someone else's browser, and until the browser says so, a
    directive that is quietly failing looks exactly like a directive that is
    quietly working. This is where it says so.

    Unauthenticated by necessity: a violation report is sent without
    credentials, so requiring a session would silence reports from the sign-in
    page, which is the page where one would matter most. It is still behind
    the network gate, still CSRF-exempt only for a body it never trusts, and
    it answers the same 204 whatever it decides, so nothing here is an oracle.
    """


    if len(request.body) > _CSP_REPORT_MAX_BYTES:
        return HttpResponse(status=204)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=204)

    for violation in _csp_violations(payload)[:10]:
        directive = str(
            violation.get("effective-directive")
            or violation.get("effectiveDirective")
            or violation.get("violated-directive")
            or ""
        )[:_CSP_FIELD_LIMIT]
        blocked = str(
            violation.get("blocked-uri") or violation.get("blockedURL") or ""
        )[:_CSP_FIELD_LIMIT]
        document = str(
            violation.get("document-uri") or violation.get("documentURL") or ""
        )[:_CSP_FIELD_LIMIT]
        if not directive:
            continue
        # One row per distinct complaint per hour. A page that violates the
        # policy on every load would otherwise write a row on every load, and
        # the thousandth copy says nothing the first did not.
        since = timezone.now() - timedelta(seconds=_CSP_REPEAT_WINDOW_SECONDS)
        already = AuditLog.objects.filter(
            action=AuditLog.Action.FAILED,
            object_type="ContentSecurityPolicy",
            metadata__directive=directive,
            metadata__blocked=blocked,
            created_at__gte=since,
        ).exists()
        if already:
            continue
        record_event(
            action=AuditLog.Action.FAILED,
            type_label="ContentSecurityPolicy",
            message=f"The browser refused {blocked or 'a resource'} under {directive}.",
            metadata={
                "directive": directive,
                "blocked": blocked,
                "document": document,
                "source": client_ip(request),
            },
        )
    return HttpResponse(status=204)


class PublicAddressView(LoginRequiredMixin, View):
    """What the public internet says about one address, as a fragment.

    A GET serves what HQ already holds and asks no one. A POST runs the lookup,
    the same application service the `lookup.address` capability runs, and
    stores what it finds.
    """

    template_name = "core/_public_address.html"

    def get(self, request):
        from application.lookup import AddressCommand, stored_address

        address = request.GET.get("address", "")
        return self._render(
            request,
            address,
            lambda principal: stored_address(
                AddressCommand(address=address), principal=principal
            ),
        )

    def post(self, request):
        from application.lookup import AddressCommand, look_up_address

        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return redirect("connection")
        address = request.POST.get("address", "")
        return self._render(
            request,
            address,
            lambda principal: look_up_address(
                AddressCommand(address=address, refresh=True), principal=principal
            ),
        )

    def _render(self, request, address, read):
        context = {"address": address, "reading": None}
        try:
            context["reading"] = read(web_principal(request.user))
        except (ValueError, AuthorizationError) as error:
            context["failure"] = str(error)
        return render(request, self.template_name, context)
