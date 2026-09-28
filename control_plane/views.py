from __future__ import annotations

import uuid
from datetime import datetime
from functools import cached_property
from typing import Any

from django.contrib import messages

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView

from application.infrastructure import (
    OperationCommand,
    PolicyError,
    declared_machines,
    delivery_targets,
    operation_summary,
    request_certificate_renewal,
    request_lifecycle,
    request_reconcile,
    request_removal,
    resource_health,
    serialize_resource,
    serialize_public_status,
)
from application.entity_links import kind_label
from application.relationships import relationships_for
from application.resource_context import (
    controller_summary,
    origin_machine,
    readout_rows,
    resource_context,
)
from application.whereabouts import whereabouts
from application.security import safe_next, web_principal
from application.pages import PageAction, PageMixin, page_context
from application.resource_capabilities import (
    VERB_LABELS,
    kind_converges,
    resource_capabilities,
)
from application.ui import counted

from core.templatetags.nav_tags import returning_to

from .models import ManagedResource, OperationRequest
from .provider_adapters.portainer import CONTAINER_KIND
from .provider_adapters.tls import CERTIFICATE_KIND
from .providers import PROVIDERS, describe_providers


# Which use case serves which verb. A ladder here meant every verb but one fell
# through to reconciliation: pressing Restart queued a reconcile, which is
# locked for a container, so the button reported a policy error and did nothing.
_OPERATION_USE_CASE = {
    OperationRequest.Action.RECONCILE: request_reconcile,
    OperationRequest.Action.RENEW: request_certificate_renewal,
}


def _web_operation(request, resource, action):
    command = OperationCommand(
        idempotency_key=f"web:{request.user.pk}:{uuid.uuid4()}",
        reason=request.POST.get("reason", "").strip(),
    )
    principal = web_principal(request.user)
    use_case = _OPERATION_USE_CASE.get(action)
    if use_case is not None:
        return use_case(command, principal=principal, current_key=resource.key)
    # Everything else is a lifecycle verb: asked for once, about something
    # already as declared. One entry point rather than one function per verb,
    # because they differ only in the word.
    return request_lifecycle(
        command, principal=principal, current_key=resource.key, action=action
    )


def _spec_value(value: Any) -> str:
    """One spec field as a person reads it: a list as its items, not its repr."""

    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _spec_rows(resource) -> dict[str, tuple[tuple[str, str], ...]]:
    """A spec as an operator reads it, split the way the form splits it.

    Shown before a destructive action, so:

    - Fields by their titles from the model, never their names.
    - An unset optional is left out rather than shown as "None".
    - Fields the provider declares routine fold away, the same split the form
      makes.
    """

    provider = PROVIDERS[resource.kind]
    fields = provider.spec_type.model_fields
    # What the readout above already printed. On anything with a handful of
    # fields the readout *is* the spec, so the disclosure repeated it whole,
    # a machine showed "What it is for" and its addresses, then offered "every
    # field of this declaration" and showed the same two again with the name.
    shown = {str(label).strip().casefold() for label, _, _ in readout_rows(resource)}
    primary: list[tuple[str, str]] = []
    advanced: list[tuple[str, str]] = []
    for name, value in resource.spec.items():
        if value is None:
            continue
        label = (
            fields[name].title or name.replace("_", " ").capitalize()
            if name in fields
            else name
        )
        if label.strip().casefold() in shown:
            continue
        rendered = _spec_value(value)
        # And not the thing the page is already titled. A machine's name is its
        # identifier here, so the last row left after the readout was the
        # heading repeated inside a disclosure offering more.
        #
        # Only when they are the same string: a declaration whose name differs
        # from its filing is telling you something, and that is the case worth
        # showing.
        if rendered == resource.key:
            continue
        row = (label, rendered)
        (advanced if name in provider.advanced_fields else primary).append(row)
    return {"primary": tuple(primary), "advanced": tuple(advanced)}


def _linked_readout(resource, relationships) -> tuple[tuple[str, str, str, tuple], ...]:
    """The readout, with each value that names a related entity as its link.

    A blank connection field is filled from the connections the relation graph
    says use this declaration.
    """

    related = {
        item.entity.label: item.entity
        for group in relationships.groups
        for item in group.items
    }
    connections = tuple(
        item.entity
        for group in relationships.groups
        for item in group.items
        if item.entity.kind == "connection"
    )
    field = PROVIDERS[resource.kind].spec_type.model_fields.get("connection_ref")
    connection_label = (field.title or "") if field is not None else ""
    rows = []
    for label, desired, observed in readout_rows(resource):
        value = str(observed or desired or "")
        if value in related:
            links = (related[value],)
        elif not value and connection_label and label == connection_label:
            links = connections
        else:
            links = ()
        rows.append((label, desired, observed, links))
    return tuple(rows)


def _removal_note(resource) -> str:
    """What this particular removal costs, if the provider says."""

    note = PROVIDERS[resource.kind].removal_note
    if note is None:
        return ""
    try:
        return note(resource.spec)
    except (KeyError, TypeError, ValueError):
        # A confirmation page that cannot render is worse than one missing a
        # sentence, and this is the page an operator uses to stop.
        return ""


class ResourceRemoveView(LoginRequiredMixin, View):
    """Ask first, then queue removal of the record itself.

    Not a row delete. What this describes lives at a provider, so dropping the
    declaration alone would leave the rewrite or proxy host in place with
    nothing in HQ pointing at it. HQ forgets its row only once a controller
    reports the provider is clear.
    """

    template_name = "control_plane/resource_confirm_remove.html"

    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        capabilities = resource_capabilities(resource)
        forget = capabilities.removal == "forget"
        return render(
            request,
            self.template_name,
            {
                "resource": resource,
                "label": kind_label(resource.kind),
                "removal_allowed": capabilities.removal != "unavailable",
                "removal_explanation": capabilities.removal_reason,
                # Said by the provider, because what breaks depends on which
                # record this is: removing one of four CAA records is
                # housekeeping, and removing the last MX record stops the
                # domain receiving mail.
                "removal_note": _removal_note(resource),
                "spec_rows": _spec_rows(resource),
                "forget": forget,
                "holds_records": bool(PROVIDERS[resource.kind].contains),
                "confirm": {
                    "url": reverse("control_plane:remove", args=[resource.key]),
                    "label": "Stop managing" if forget else "Remove",
                    "cancel_url": resource.get_absolute_url(),
                },
                **page_context(
                    f"Remove {resource.key}?",
                    kind_label(resource.kind),
                ),
            },
        )

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        try:
            result = request_removal(
                OperationCommand(
                    idempotency_key=f"web:{request.user.pk}:{uuid.uuid4()}",
                    reason=request.POST.get("reason", "").strip(),
                ),
                principal=web_principal(request.user),
                current_key=resource.key,
            )
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect("control_plane:detail", key=key)
        if "forgotten" in result:
            # Nothing was queued because nothing exists at the provider that HQ
            # made. Saying "queued removal" here would promise a deletion that
            # is neither happening nor wanted.
            released = len(result["released"])
            messages.success(
                request,
                f"Stopped managing “{result['forgotten']}”"
                + (
                    f" and {counted(released, 'record declaration', 'record declarations')} in it"
                    if released
                    else ""
                )
                + ". Nothing changed at the provider.",
            )
            return redirect("control_plane:list")
        verb = "Queued" if result["queued"] else "Already queued"
        messages.success(
            request,
            f"{verb} removal of “{resource.key}”. HQ drops it once the "
            "provider is clear.",
        )
        return redirect("control_plane:detail", key=key)


class InfrastructureListView(PageMixin, LoginRequiredMixin, ListView):
    model = ManagedResource
    template_name = "control_plane/resource_list.html"
    context_object_name = "resources"
    page_title = "Infrastructure"
    page_lede = "Declared resources and their last observed state."

    def get_page_actions(self):
        return (
            PageAction("Add", reverse("control_plane:create"), primary=True),
            PageAction("Services", reverse("control_plane:services")),
            PageAction("Provider schemas", reverse("control_plane:providers")),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Read once for the whole page. Every row that forwards somewhere asks
        # the same question of the same few tables, and asked per row it is a
        # query budget that grows with the estate.
        machines = declared_machines()
        targets = delivery_targets()
        at = whereabouts(machines)
        for resource in context["resources"]:
            resource.control_health = resource_health(resource)
            # Where it sends traffic, named rather than addressed, matching
            # what the resource's own page has always said.
            resource.origin_machine = origin_machine(resource, machines, at, targets)
            # What it is, in the provider's own words.
            rows = readout_rows(resource)
            resource.summary = rows[0][1] or rows[0][2] if rows else ""
            resource.converges = kind_converges(resource.kind)
        context["operations"] = OperationRequest.objects.select_related("resource")[:12]
        context["provider_catalog"] = describe_providers()
        return context


class InfrastructureDetailView(PageMixin, LoginRequiredMixin, DetailView):
    model = ManagedResource
    slug_field = "key"
    slug_url_kwarg = "key"
    template_name = "control_plane/resource_detail.html"
    context_object_name = "resource"

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        home = self.object.get_absolute_url()
        if home != request.path:
            # A kind with a page of its own, such as a machine, lives there.
            return redirect(home)
        return super().get(request, *args, **kwargs)

    @cached_property
    def derived(self):
        return resource_context(self.object)

    @cached_property
    def capabilities(self):
        return self.derived.capabilities

    @cached_property
    def container(self):
        """What the sweep knows about a watched container, and nothing otherwise."""

        if self.object.kind != CONTAINER_KIND:
            return None
        from .container_views import container_detail

        return container_detail(self.object, self.request)

    def get_page_title(self):
        # A container by its own name: its machine is the trail above it.
        if self.object.kind == CONTAINER_KIND and self.object.spec.get("name"):
            return self.object.spec["name"]
        return self.object.key

    def get_page_lede(self):
        return kind_label(self.object.kind)

    @cached_property
    def all_relationships(self):
        """Every relation, for what the page derives from them (its home)."""

        return relationships_for(
            f"resource:{self.object.key}", principal=web_principal(self.request.user)
        )

    @cached_property
    def relationships(self):
        """The relations the section shows: less what the page says elsewhere."""

        found = self.all_relationships
        if self.object.kind == CONTAINER_KIND:
            from application.page_relations import for_container

            return for_container(found)
        return found

    @cached_property
    def home(self):
        """The machine, service or domain this declaration belongs to, if any."""

        return next(
            (
                item.entity
                for group in self.all_relationships.groups
                for item in group.items
                if item.entity.kind in ("machine", "service", "zone")
            ),
            None,
        )

    def get_page_trail(self):
        crumbs = []
        if self.home and self.home.url:
            crumbs.append((self.home.label, self.home.url))
        return tuple(crumbs)

    def get_page_actions(self):
        if self.capabilities.removal_pending:
            return ()
        key = self.object.key
        capabilities = self.capabilities
        actions = [
            PageAction(
                VERB_LABELS[verb],
                reverse(f"control_plane:{verb}", args=[key]),
                method="post",
                primary=verb == "renew",
                disabled=not allowed.enabled,
                title="" if allowed.enabled else allowed.reason,
            )
            for verb, allowed in capabilities.page_actions
        ]
        actions.append(
            PageAction(
                "Edit",
                returning_to(
                    reverse("control_plane:edit", args=[key]),
                    self.request.get_full_path(),
                ),
            )
        )
        if PROVIDERS[self.object.kind].material_form:
            actions.append(
                PageAction(
                    "Replace certificate"
                    if getattr(self.object, "material", None)
                    else "Upload certificate",
                    reverse("control_plane:upload_certificate", args=[key]),
                    primary=True,
                )
            )
        if capabilities.removal != "unavailable":
            actions.append(
                PageAction(
                    "Stop managing" if capabilities.removal == "forget" else "Remove",
                    reverse("control_plane:remove", args=[key]),
                    danger=True,
                )
            )
        return tuple(actions)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        capabilities = self.capabilities
        context["capabilities"] = capabilities
        context["controller"] = controller_summary(
            capabilities.actions, lambda verb: VERB_LABELS.get(verb, verb.replace("-", " ").capitalize())
        )
        derived = self.derived
        context["control_health"] = derived.health
        context["sync_state"] = "in_sync" if derived.in_sync else "pending"
        # What this resource does, said by its own provider.
        context["label"] = kind_label(self.object.kind)
        context["service_links"] = derived.service_links
        # Where this resource sends traffic, when it sends it anywhere, and the
        # machine running the provider that manages it.
        context["origin_machine"] = derived.origin_machine
        context["provider_machine"] = derived.provider_machine
        if self.container is not None:
            context["container"] = self.container
        context["removal_pending"] = capabilities.removal_pending
        # A change a credential asked for that nobody has answered, said on the
        # page an operator opens when a declaration has not moved.
        context["awaiting_approval"] = derived.awaiting_approval
        # Nothing for a container: its panel is the sweep's answer and a
        # container declares identity and nothing else.
        context["readout_rows"] = (
            ()
            if self.object.kind == CONTAINER_KIND
            else _linked_readout(self.object, self.relationships)
        )
        context["relationships"] = self.relationships
        context["spec_rows"] = _spec_rows(self.object)
        context["renewal_at"] = derived.expiry.renewal_at if derived.expiry else None
        context["operations"] = [
            operation_summary(operation)
            for operation in self.object.operations.all()[:20]
        ]
        for operation in context["operations"]:
            operation["created_at"] = datetime.fromisoformat(operation["created_at"])
            if operation["completed_at"]:
                operation["completed_at"] = datetime.fromisoformat(
                    operation["completed_at"]
                )
        context["resolved_spec"] = derived.resolved_spec
        context["resolution_error"] = derived.resolution_error
        context["display_consumers"] = derived.display_consumers
        context["diagnostic_status"] = serialize_public_status(self.object.status)
        return context


# What each controller verb is called in a sentence. One entry per verb, so a
# new verb needs no new view class.
OPERATION_PHRASE = {
    OperationRequest.Action.RECONCILE: "reconciliation",
    OperationRequest.Action.RENEW: "certificate renewal",
    OperationRequest.Action.DELETE: "removal",
    OperationRequest.Action.RESTART: "a restart",
    OperationRequest.Action.START: "a start",
    OperationRequest.Action.STOP: "a stop",
    OperationRequest.Action.APPROVE_ROUTES: "route approval",
}


class OperationView(LoginRequiredMixin, View):
    """Ask the controller for one action on one resource.

    The action comes from the URL rather than from the class, so adding a verb
    is a route and a phrase rather than another view that does what this one
    already does.
    """

    action = OperationRequest.Action.RECONCILE

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        # Back where the verb was offered. These forms are on pages that show
        # the fact the verb answers (a machine's routes, a service's
        # container) and they have been sending `next` all along while this
        # view returned to the resource record regardless. Validated through
        # the shared helper, so the field cannot become an open redirect.
        destination = safe_next(
            request, fallback=reverse("control_plane:detail", kwargs={"key": key})
        )
        try:
            result = _web_operation(request, resource, self.action)
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect(destination)
        verb = "Queued" if result["queued"] else "Already queued"
        phrase = OPERATION_PHRASE.get(self.action, self.action)
        messages.success(request, f"{verb} {phrase} for “{resource.key}”.")
        return redirect(destination)


class CertificateDownloadView(LoginRequiredMixin, View):
    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        certificate_pem = resource.status.get("certificate_pem", "")
        if resource.kind != CERTIFICATE_KIND or not certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "No verified certificate to download."},
                status=404,
            )
        if "PRIVATE KEY-----" in certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "Refused: the certificate file contains a private key."},
                status=500,
            )
        response = HttpResponse(certificate_pem, content_type="application/x-pem-file")
        response["Content-Disposition"] = (
            f'attachment; filename="{resource.key}-public.pem"'
        )
        return response


class ResourceReportDownloadView(LoginRequiredMixin, View):
    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        payload = {
            "schema_version": 1,
            "resource": serialize_resource(resource),
            "operations": [
                {
                    "id": str(operation.id),
                    "action": operation.action,
                    "state": operation.state,
                    "created_at": operation.created_at.isoformat(),
                    "result": operation.result,
                }
                for operation in resource.operations.all()[:50]
            ],
        }
        response = JsonResponse(payload, json_dumps_params={"indent": 2})
        response["Content-Disposition"] = (
            f'attachment; filename="{resource.key}-status.json"'
        )
        return response


class ProviderSchemaView(LoginRequiredMixin, View):
    def get(self, request):
        return JsonResponse(describe_providers(), json_dumps_params={"indent": 2})
