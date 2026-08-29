"""Dashboard + audit-log views."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from urllib.parse import quote

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import formats
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView, View

from application.connections import link_choices, outward_links
from application.command_center import command_center
from application.dashboard import operating_snapshot, work_queue
from application.projection import projection_scope
from application.plugins import plugin_health
from application.search import global_search
from application.security import AuthorizationError, safe_next, web_principal
from application.tables import TableFilter, TableListMixin, TableSort
from application.ui import ListRow
from contacts.d1 import (
    D1Error,
    get_dashboard_state,
    search_submissions,
)
from .audit import record_event
from .middleware import DEMO_SESSION_KEY
from .models import AuditLog
from .network import client_ip


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


class DashboardLinkChoiceView(LoginRequiredMixin, View):
    """Choose which outward links the dashboard shows, for this operator only.

    A preference, so it is stored the same way starring a domain is and reaches
    no spec, no generation and no controller: the world does not change because
    somebody decided which shortcuts they want.
    """

    def post(self, request):
        from application.connections import link_choices
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
        try:
            recent_contacts, unread_contacts = get_dashboard_state(limit=4)
            contacts = (unread_contacts, "ok")
        except D1Error:
            recent_contacts = []
            contacts = (0, "unavailable")
        snapshot = operating_snapshot(contacts=contacts)
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

        # Consoles come from the connections a controller reported. Anything
        # else an operator wants here is a fact about their installation and is
        # named in their environment: an address written into this file is
        # published to everyone who clones it and true for nobody else.
        external_links, external_curated = outward_links(self.request.user)
        external_choices = link_choices(self.request.user)
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
            external_choices=external_choices,
            external_curated=external_curated,
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
            profile_action_count=snapshot["priority_count"],
            show_action_count=True,
            this_year=snapshot["year"],
            dashboard_cards=snapshot["cards"],
        )
        return ctx


class ActionItemsView(LoginRequiredMixin, TemplateView):
    """The full human surface for HQ's one composed attention queue."""

    template_name = "action_items.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip().casefold()
        status = self.request.GET.get("status", "").strip()
        source = self.request.GET.get("source", "").strip()
        with projection_scope():
            all_items = work_queue()
        items = [
            item
            for item in all_items
            if (not status or item["status"] == status)
            and (not source or item["source_id"] == source)
            and (
                not query
                or query
                in " ".join(
                    (item["source"], item["label"], item["detail"])
                ).casefold()
            )
        ]
        sources = tuple(
            {item["source_id"]: item["source"] for item in all_items}.items()
        )
        context.update(
            action_items=items,
            action_item_total=sum(item["count"] for item in items),
            action_item_group_total=len(items),
            profile_action_count=sum(item["count"] for item in all_items),
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
    the only trace it leaves -- and it is recorded because an operator who
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
        # mark while it is on -- a paragraph on every flip is a third telling
        # of something already said twice.
        return redirect(safe_next(request, fallback=reverse("dashboard")))


class ActionItemCountView(LoginRequiredMixin, View):
    """Compute the header badge only when an operator opens its menu."""

    def get(self, request):
        with projection_scope():
            count = sum(item["count"] for item in work_queue())
        return JsonResponse({"count": count})


class SearchView(LoginRequiredMixin, TemplateView):
    template_name = "search.html"
    result_limit = 8

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()
        groups: list[dict] = []
        contacts: list = []
        total = 0
        principal = web_principal(self.request.user)
        if q:
            outcome = global_search(
                q,
                principal=principal,
                limit_per_scope=self.result_limit,
            )
            groups = outcome["groups"]
            total = outcome["total"]
            try:
                contacts = search_submissions(q, limit=self.result_limit)
            except D1Error:
                contacts = []
            total += len(contacts)
        discovery = command_center(q, principal=principal)
        ctx.update(
            q=q,
            search_query=q,
            groups=groups,
            contacts=contacts,
            total=total,
            discovered_resources=discovery["resources"],
            discovered_commands=discovery["commands"],
            discovered_connections=discovery["connections"],
            discovered_views=discovery["views"],
            discovered_checks=discovery["checks"],
            discovery_total=(
                len(discovery["resources"])
                + len(discovery["commands"])
                + len(discovery["connections"])
                + len(discovery["views"])
                + len(discovery["checks"])
            ),
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


class ConnectionView(LoginRequiredMixin, TemplateView):
    """Why this request was allowed to arrive, layer by layer.

    A page rather than only a dialog, for the same reason every other dialog
    here has one behind it: the panel is an enhancement, and the answer has to
    exist for somebody who followed the link with script off, or who wants to
    send it to themselves.
    """

    template_name = "core/connection.html"

    def get_context_data(self, **kwargs):
        from application.connection import (
            addresses_of,
            addresses_of_hq,
            connection as describe,
            headers_of,
            hops_of,
        )
        from application.connection_security import observed_ingress_control

        context = super().get_context_data(**kwargs)
        # Provider observations are cached facts. The request explanation may
        # derive from them, but opening the panel never probes NPM or handles a
        # credential.
        edge = observed_ingress_control(self.request.get_host())
        found = describe(self.request, edge=edge)
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

    Unauthenticated by necessity -- a violation report is sent without
    credentials, so requiring a session would silence reports from the sign-in
    page, which is the page where one would matter most. It is still behind
    the network gate, still CSRF-exempt only for a body it never trusts, and
    it answers the same 204 whatever it decides, so nothing here is an oracle.
    """

    from django.utils import timezone

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


class PublicAddressView(LoginRequiredMixin, TemplateView):
    """What the public internet says about one address, fetched on demand.

    A fragment rather than a page, and on demand rather than on render, for a
    reason the connection page already enforces elsewhere: nothing about
    locating HQ may put a lookup in the path of drawing it. The operator opens
    the disclosure, and only then does anything leave this machine.
    """

    template_name = "core/_public_address.html"

    def get_context_data(self, **kwargs):
        from application.lookup import AddressCommand, look_up_address

        context = super().get_context_data(**kwargs)
        try:
            # The same application service the `lookup.address` capability
            # runs. A second path to the same answer is a second place for it
            # to be subtly different, and this one is a delivery adapter like
            # the command form and the machine API are.
            context["reading"] = look_up_address(
                AddressCommand(address=self.request.GET.get("address", "")),
                principal=web_principal(self.request.user),
            )
        except (ValueError, AuthorizationError) as error:
            context["reading"] = None
            context["failure"] = str(error)
        return context
