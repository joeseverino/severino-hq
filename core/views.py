"""Dashboard + audit-log views."""

from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
from urllib.parse import quote

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import formats
from django.views.generic import DetailView, ListView, TemplateView

from application.dashboard import operating_snapshot
from application.plugins import plugin_health
from application.search import global_search
from application.security import web_principal
from application.tables import TableFilter, TableListMixin, TableSort
from application.ui import ListRow
from contacts.d1 import (
    D1Error,
    get_recent_submissions,
    search_submissions,
)
from .models import AuditLog


class ThrottledLoginView(LoginView):
    """The password form, with a cost attached to guessing at it.

    Refused *before* the credentials are checked. Validating first and
    discarding the result would still answer the attacker's actual question --
    response timing, and the difference between "no such user" and "locked",
    both leak whether a guess was close -- and would spend a password hash per
    attempt doing it, which is the expensive operation an attacker wants to
    provoke.

    The message names no account and no address. It says the door is shut and
    when it reopens, which is everything a locked-out operator needs and
    nothing an attacker can use to tell whether they found a real username.
    """

    template_name = "auth/login.html"

    @property
    def sso_only(self) -> bool:
        return settings.SEVERINO_OIDC_ENABLED and not settings.SEVERINO_PASSWORD_LOGIN_ENABLED

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
            nxt = request.GET.get("next")
            # Checked here even though the provider library checks it again
            # before use. A destination is only carried forward if it points
            # back at this host, so nothing downstream has to be trusted to
            # notice that it does not.
            if nxt and url_has_allowed_host_and_scheme(
                nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(f"{target}?next={quote(nxt)}")
            return redirect(target)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        from .network import client_ip
        from .throttle import lockout

        if self.sso_only:
            # There is no backend to check it against, so this could only ever
            # fail -- but failing here means it is never carried further, and
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
            f"{state.minutes_remaining} minute"
            f"{'s' if state.minutes_remaining != 1 else ''}.",
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


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        try:
            recent_contacts = get_recent_submissions(limit=4)
        except D1Error:
            recent_contacts = []
        snapshot = operating_snapshot()
        for project in snapshot["active_projects"]:
            project["updated_at"] = datetime.fromisoformat(project["updated_at"])
        for collection in (snapshot["draft_content"], snapshot["recent_published"]):
            for item in collection:
                item["updated_at"] = datetime.fromisoformat(item["updated_at"])
                if item["published_at"]:
                    item["published_at"] = date.fromisoformat(item["published_at"])
        for record in snapshot["docs_needing_review"]:
            if record["last_reviewed"]:
                record["last_reviewed"] = date.fromisoformat(record["last_reviewed"])
        for event in snapshot["recent_activity"]:
            event["created_at"] = datetime.fromisoformat(event["created_at"])
        # No mapping step: every queue entry already carries the link to the
        # filtered list that shows it, supplied by the domain that raised it.
        action_queue = snapshot["priority"]

        # Live infra status is NOT computed here — HQ links out to Uptime Kuma
        # on the VPS rather than duplicating a status checker.
        external_links = [
            {
                "label": "Live status",
                "sub": "Uptime Kuma · VPS",
                "href": "https://status.jseverino.com",
            },
            {
                "label": "Health endpoint",
                "sub": "liveness",
                "href": "https://health.jseverino.com",
            },
            {"label": "Portainer", "sub": "containers", "href": "http://admin.homelab"},
            {
                "label": "Public site",
                "sub": "jseverino.com",
                "href": "https://jseverino.com",
            },
        ]

        # Projected here, not in operating_snapshot(): that snapshot is also the
        # MCP payload, and a transport contract must not carry a UI shape.
        contact_rows = [
            ListRow(
                title=submission["name"],
                detail=submission["status"],
                meta=submission["created_at"],
                url=reverse("contacts:detail", args=[submission["id"]]),
            )
            for submission in recent_contacts
        ]
        content_rows = [
            ListRow(
                title=item["title"],
                detail=item["content_type_label"],
                meta=formats.date_format(item["updated_at"], "M j"),
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
        docs_rows = [
            ListRow(
                title=record["title"],
                detail=record["doc_id"],
                meta=(
                    formats.date_format(record["last_reviewed"], "M j")
                    if record["last_reviewed"]
                    else "never"
                ),
                url=reverse("docs_index:detail", args=[record["doc_id"]]),
            )
            for record in snapshot["docs_needing_review"]
        ]

        ctx.update(
            recent_contacts=contact_rows,
            content_rows=content_rows,
            published_rows=published_rows,
            docs_rows=docs_rows,
            unread_contacts_count=snapshot["kpis"]["unread_contacts"],
            active_project_count=snapshot["kpis"]["active_projects"],
            active_projects=snapshot["active_projects"],
            project_opportunities_count=snapshot["kpis"]["projects_needing_output"],
            external_links=external_links,
            draft_content=snapshot["draft_content"],
            draft_content_count=snapshot["kpis"]["draft_content"],
            published_content_count=snapshot["kpis"]["published_content"],
            recent_published=snapshot["recent_published"],
            expenses_ytd_total=snapshot["kpis"]["expenses_total"],
            expenses_ytd_count=snapshot["kpis"]["expenses_count"],
            deductible_ytd_total=snapshot["kpis"]["deductible_total"],
            docs_needing_review=snapshot["docs_needing_review"],
            docs_needing_review_count=snapshot["kpis"]["docs_needing_review"],
            recent_audit=snapshot["recent_activity"],
            action_queue=action_queue,
            action_queue_count=snapshot["priority_count"],
            action_queue_group_count=snapshot["priority_group_count"],
            this_year=snapshot["year"],
            dashboard_cards=snapshot["cards"],
        )
        return ctx


class SearchView(LoginRequiredMixin, TemplateView):
    template_name = "search.html"
    result_limit = 8

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()
        groups: list[dict] = []
        contacts: list = []
        total = 0
        if q:
            outcome = global_search(
                q,
                principal=web_principal(self.request.user),
                limit_per_scope=self.result_limit,
            )
            groups = outcome["groups"]
            total = outcome["total"]
            try:
                contacts = search_submissions(q, limit=self.result_limit)
            except D1Error:
                contacts = []
            total += len(contacts)
        ctx.update(
            q=q,
            search_query=q,
            groups=groups,
            contacts=contacts,
            total=total,
        )
        return ctx


class AuditLogListView(TableListMixin, LoginRequiredMixin, ListView):
    model = AuditLog
    template_name = "core/auditlog_list.html"
    context_object_name = "events"
    paginate_by = 50
    table_search_scope = "audit"
    table_filters = (
        TableFilter("action", "Action", "action", AuditLog.Action.choices),
    )
    table_sorts = (
        TableSort("-created_at", "Newest event", "-created_at"),
        TableSort("created_at", "Oldest event", "created_at"),
        TableSort("action", "Action", "action"),
        TableSort("-action", "Action reverse", "-action"),
        TableSort("object_type", "Object type", "object_type"),
        TableSort("-object_type", "Object type reverse", "-object_type"),
        TableSort("user__username", "User A–Z", "user__username"),
        TableSort("-user__username", "User Z–A", "-user__username"),
        TableSort("message", "Message A–Z", "message"),
        TableSort("-message", "Message Z–A", "-message"),
    )
    table_default_sort = "-created_at"
    table_search_placeholder = "Search objects, operation IDs, and messages…"

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user")
        return self.apply_table_query(qs)


class AuditLogDetailView(LoginRequiredMixin, DetailView):
    """One event, in full, and what sits either side of it.

    The list can only ever show a line per event. What an audit trail is
    actually consulted for is the question behind the line -- which field
    moved, what the value was before, what else the same action touched -- and
    none of that fits in a row. So every row leads here.
    """

    model = AuditLog
    template_name = "core/auditlog_detail.html"
    context_object_name = "event"

    def get_queryset(self):
        return AuditLog.objects.select_related("user")

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
            key: value for key, value in sorted(event.metadata.items())
            if key != "changes"
        }

        # The rest of the same operation -- what `operation_id` is for. One
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
        return context
