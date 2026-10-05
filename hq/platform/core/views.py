"""Dashboard + audit-log views."""

from __future__ import annotations

from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from hq.platform.application import fragments
from hq.platform.application.agent_access import set_agents_paused
from hq.platform.application.appearance import set_theme
from hq.platform.application.avatars import avatar_of
from hq.platform.application.command_center import command_center
from hq.platform.application.projection import projection_scope
from hq.platform.application.search import global_search
from hq.platform.application.security import AuthorizationError, safe_next, web_principal
from hq.platform.application.pages import PageMixin
from hq.platform.application.ui import counted
from hq.domains.contacts import inbox
from .audit import record_event
from .middleware import DEMO_SESSION_KEY
from .models import AuditLog


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

        Signing in is already a redirect to the identity provider, so stopping
        to confirm that is a click that decides nothing.

        Except after signing out, where bouncing would immediately return the
        still-valid provider session and make the sign-out look broken. There,
        the page is shown so leaving is possible.
        """

        if self.sso_only and not {"signed_out", "sso_failed"} & set(request.GET):
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

    def get_context_data(self, **kwargs):
        from .oidc import SSO_FAILURE_SESSION_KEY

        context = super().get_context_data(**kwargs)
        if "sso_failed" in self.request.GET:
            context["sso_failure"] = self.request.session.pop(
                SSO_FAILURE_SESSION_KEY, "Signing in did not finish."
            )
        return context

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


class DemoModeView(View):
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


class AgentAccessView(View):
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


class ThemeView(View):
    """Choose system, light or dark. The form sends a choice, never a toggle."""

    def post(self, request):
        try:
            set_theme(
                request.POST.get("theme", ""),
                principal=web_principal(request.user),
                user=request.user,
            )
        except ValueError:
            # A fixed reply: the service's own message is for logs and callers
            # in-process, not something a request gets to read back.
            return HttpResponseBadRequest("Choose system, light or dark.")
        return redirect(safe_next(request, fallback=reverse("dashboard")))


class AvatarView(View):
    """The picture of whoever is asking, and nobody else's.

    The address carries the picture's digest, so a new picture is a new
    address and this one never changes: a browser may keep it for good. The
    reply forbids everything a document could do, in case a file that passed
    as an image is ever opened as one.
    """

    def get(self, request, digest):
        avatar = avatar_of(request.user)
        if avatar is None or avatar.digest != digest:
            raise Http404
        response = HttpResponse(bytes(avatar.image), content_type=avatar.content_type)
        response["Cache-Control"] = "private, max-age=31536000, immutable"
        response["Content-Disposition"] = "inline"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class AgentPolicyView(PageMixin, TemplateView):
    """Capability policy. Reads from matrix(), writes through apply_changes()."""

    template_name = "core/agent_policy.html"
    page_title = "Agents"

    def get_context_data(self, **kwargs):
        from datetime import timedelta


        from hq.platform.application import capability_policy
        from hq.platform.application.approvals import awaiting_ids

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
        from hq.platform.application import capability_policy

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


class SearchView(PageMixin, TemplateView):
    template_name = "search.html"
    page_title = "Command Center"
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
        palette_request = fragments.requested(self.request) == "palette"
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


class ApprovalEntryView(View):
    """A stable link to a held request's audit entry."""

    def get(self, request, approval_id):
        from hq.platform.application.approvals import entry_event

        event = entry_event(approval_id)
        if event is None:
            return redirect(f"{reverse('core:audit_list')}?awaiting=1")
        return redirect("core:audit_detail", pk=event.pk)


class ConnectionView(PageMixin, TemplateView):
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
        from hq.platform.application.request_path import request_path

        context = super().get_context_data(**kwargs)
        # Provider observations are cached facts. The request explanation may
        # derive from them, but opening the panel never probes NPM or handles a
        # credential.
        found = request_path(self.request)
        context["request_path"] = found
        context["connection"] = found.connection
        return context


class PublicAddressView(View):
    """What the public internet says about one address, as a fragment.

    A GET serves what HQ already holds and asks no one. A POST runs the lookup,
    the same application service the `lookup.address` capability runs, and
    stores what it finds.
    """

    template_name = "core/_public_address.html"

    def get(self, request):
        from hq.platform.application.lookup import AddressCommand, stored_address

        address = request.GET.get("address", "")
        return self._render(
            request,
            address,
            lambda principal: stored_address(
                AddressCommand(address=address), principal=principal
            ),
        )

    def post(self, request):
        from hq.platform.application.lookup import AddressCommand, look_up_address

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
