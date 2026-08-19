from __future__ import annotations

import uuid
import math
from datetime import datetime, timedelta, timezone

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from application.infrastructure import (
    ManagedResourceCommand,
    NotFoundError,
    OperationCommand,
    PolicyError,
    certificate_renewal_allowed,
    controller_contract,
    operation_summary,
    request_certificate_renewal,
    request_reconcile,
    request_removal,
    resource_health,
    save_managed_resource,
    serialize_resource,
    serialize_public_status,
)
from application.inventory import (
    AdoptServiceCommand,
    adopt_service,
    inventory_state,
    unmanaged_services,
)
from application.provider_forms import ResourceIdentityForm, spec_form_class
from application.security import web_principal
from application.services import find_service, service_catalog

from .models import ManagedResource, OperationRequest
from .providers import (
    PROVIDERS,
    SERVICE_FACETS,
    controller_action_policy,
    describe_providers,
)


def _web_operation(request, resource, action):
    command = OperationCommand(
        idempotency_key=f"web:{request.user.pk}:{uuid.uuid4()}",
        reason=request.POST.get("reason", "").strip(),
    )
    if action == "renew":
        return request_certificate_renewal(
            command,
            principal=web_principal(request.user),
            current_key=resource.key,
        )
    return request_reconcile(
        command,
        principal=web_principal(request.user),
        current_key=resource.key,
    )


class ResourceFormView(LoginRequiredMixin, View):
    """Declare or amend one resource, on a form its provider generates.

    The write goes through ``save_managed_resource`` -- the same use case the
    API and the MCP call -- so the capability check, the spec validation, the
    generation bump and the audit record are the ones that already existed. This
    view supplies a form and a redirect and decides nothing else.
    """

    template_name = "control_plane/resource_form.html"

    def _existing(self, key):
        return get_object_or_404(ManagedResource, key=key) if key else None

    def _kind(self, request, resource):
        if resource:
            return resource.kind
        return request.GET.get("kind") or request.POST.get("kind") or ""

    def get(self, request, key=None):
        resource = self._existing(key)
        kind = self._kind(request, resource)
        if kind not in PROVIDERS:
            return render(
                request,
                "control_plane/resource_kind.html",
                {"providers": describe_providers()["providers"]},
            )
        hostname = request.GET.get("hostname", "").strip()
        seed = PROVIDERS[kind].seed
        return render(
            request,
            self.template_name,
            {
                "kind": kind,
                "resource": resource,
                "label": PROVIDERS[kind].label or kind,
                "summary": PROVIDERS[kind].summary,
                # Only when editing. Creating something asks what HQ cannot
                # know and nothing else: a name it can derive, and a pause
                # switch for a thing that does not exist yet, are not questions.
                "identity": (
                    ResourceIdentityForm(
                        initial={"key": resource.key, "enabled": resource.enabled}
                    )
                    if resource
                    else None
                ),
                "spec": spec_form_class(kind, lock_identity=bool(resource))(
                    initial=(
                        resource.spec
                        if resource
                        else (seed(hostname) if seed and hostname else None)
                    )
                ),
            },
        )

    def post(self, request, key=None):
        resource = self._existing(key)
        kind = self._kind(request, resource)
        if kind not in PROVIDERS:
            raise Http404("Unknown provider kind.")
        identity = ResourceIdentityForm(request.POST) if resource else None
        spec = spec_form_class(kind, lock_identity=bool(resource))(
            request.POST, initial=resource.spec if resource else None
        )
        if (identity is None or identity.is_valid()) and spec.is_valid():
            try:
                result = save_managed_resource(
                    ManagedResourceCommand(
                        key=(
                            identity.cleaned_data["key"] or resource.key
                            if identity
                            else _derived_key(kind, spec.spec)
                        ),
                        kind=kind,
                        spec=spec.spec,
                        # A thing being created is a thing you want applied.
                        enabled=identity.cleaned_data["enabled"] if identity else True,
                    ),
                    principal=web_principal(request.user),
                    current_key=resource.key if resource else None,
                )
            except (PolicyError, DjangoValidationError) as exc:
                spec.add_error(None, _readable_error(exc))
            else:
                saved = result["resource"]["key"]
                messages.success(
                    request,
                    f"{'Added' if result['created'] else 'Updated'} “{saved}”. "
                    "HQ will apply it at the provider within about a minute.",
                )
                return redirect("control_plane:detail", key=saved)
        return render(
            request,
            self.template_name,
            {
                "kind": kind,
                "resource": resource,
                "label": PROVIDERS[kind].label or kind,
                "summary": PROVIDERS[kind].summary,
                "identity": identity,
                "spec": spec,
            },
        )


def _suggested_key(hostname: str, kind: str) -> str:
    """A name that says what this is, offered rather than imposed.

    Onboarding a service should not stop to ask what to call three rows in a
    table the operator did not know existed. The field stays editable, because
    the key is stable and permanent and sometimes the obvious name is taken.
    """

    if not hostname:
        return ""
    facet = PROVIDERS[kind].facet or kind
    # Dots become separators before slugify sees them. Left alone, slugify
    # deletes them, and "app.example.com" suggests the key "appexamplecom" --
    # a permanent, unreadable name for the sake of one substitution.
    return slugify(f"{hostname}-{facet}".replace(".", "-"))[:180]


def _derived_key(kind: str, spec: dict) -> str:
    """A name for a declaration the operator did not want to name.

    Asked for one, the form stopped to demand an identifier for a row in a table
    nobody had mentioned yet. The hostname the spec already carries is a better
    name than anything that would have been typed, and the provider says how to
    read it out.
    """

    provider = PROVIDERS[kind]
    hostnames = provider.hostnames(spec) if provider.hostnames else ()
    base = _suggested_key(hostnames[0], kind) if hostnames else slugify(kind)
    if not ManagedResource.objects.filter(key=base).exists():
        return base
    for suffix in range(2, 100):
        candidate = f"{base[:176]}-{suffix}"
        if not ManagedResource.objects.filter(key=candidate).exists():
            return candidate
    return base


def _readable_error(exc) -> str:
    messages_found = getattr(exc, "messages", None)
    return " ".join(messages_found) if messages_found else str(exc)


class AdoptView(LoginRequiredMixin, View):
    """Bring something the provider already holds under HQ's management.

    One click, no form. The spec is read back out of the live record, so the
    declaration starts equal to the world and the first reconciliation changes
    nothing -- which is the only reason adopting is safe to do without asking
    the operator to confirm every field first.
    """

    def post(self, request, hostname):
        try:
            result = adopt_service(
                AdoptServiceCommand(hostname=hostname),
                principal=web_principal(request.user),
            )
        except (NotFoundError, PolicyError, DjangoValidationError) as exc:
            messages.error(request, _readable_error(exc))
            return redirect("control_plane:services")
        adopted = ", ".join(result["adopted"])
        messages.success(
            request,
            f"Adopted {hostname} as {adopted}, exactly as configured now. "
            "Nothing changed at the provider.",
        )
        return redirect("control_plane:service", hostname=result["hostname"])


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
        allowed, explanation = controller_action_policy(
            resource.kind, OperationRequest.Action.DELETE
        )
        return render(
            request,
            self.template_name,
            {
                "resource": resource,
                "label": PROVIDERS[resource.kind].label or resource.kind,
                "removal_allowed": allowed,
                "removal_explanation": explanation,
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
        verb = "Queued" if result["queued"] else "Already queued"
        messages.success(
            request,
            f"{verb} removal of “{resource.key}”. HQ forgets it once the "
            "controller confirms the provider is clear.",
        )
        return redirect("control_plane:detail", key=key)


class ServiceListView(LoginRequiredMixin, TemplateView):
    """The hostname view of the same declarations the resource list shows."""

    template_name = "control_plane/service_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["services"] = service_catalog()
        # The column headers come from the providers, so a provider that
        # declares a new facet gets a column without this template being touched.
        context["facets"] = SERVICE_FACETS
        # Everything the providers hold that no declaration accounts for. Shown
        # beside the managed services rather than on a page of its own: a
        # hostname HQ does not manage is still a hostname that is serving, and
        # hiding it is how a console ends up describing only the tidy half of
        # the estate.
        context["unmanaged"] = unmanaged_services()
        context["inventory"] = inventory_state()
        return context


class ServiceDetailView(LoginRequiredMixin, TemplateView):
    template_name = "control_plane/service_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        service = find_service(self.kwargs["hostname"])
        if service is None:
            raise Http404(f"No service is declared for {self.kwargs['hostname']}.")
        context["service"] = service
        return context


class InfrastructureListView(LoginRequiredMixin, ListView):
    model = ManagedResource
    template_name = "control_plane/resource_list.html"
    context_object_name = "resources"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for resource in context["resources"]:
            resource.control_health = resource_health(resource)
        context["operations"] = OperationRequest.objects.select_related("resource")[:12]
        context["provider_catalog"] = describe_providers()
        return context


class InfrastructureDetailView(LoginRequiredMixin, DetailView):
    model = ManagedResource
    slug_field = "key"
    slug_url_kwarg = "key"
    template_name = "control_plane/resource_detail.html"
    context_object_name = "resource"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        allowed, explanation = certificate_renewal_allowed(self.object)
        context["renewal_allowed"] = allowed
        context["renewal_explanation"] = explanation
        context["control_health"] = resource_health(self.object)
        reconcile_allowed, reconcile_explanation = controller_action_policy(
            self.object.kind, OperationRequest.Action.RECONCILE
        )
        context["reconcile_allowed"] = reconcile_allowed
        context["reconcile_explanation"] = reconcile_explanation
        context["controller_capability"] = describe_providers()["controller"][
            "capabilities"
        ][self.object.kind]["actions"]
        context["sync_state"] = (
            "in_sync"
            if self.object.generation == self.object.observed_generation
            else "pending"
        )
        context["days_left"] = None
        context["renewal_at"] = None
        not_after = self.object.status.get("not_after")
        if not_after:
            try:
                expiry = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                context["days_left"] = max(
                    0,
                    math.ceil(
                        (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
                    ),
                )
                context["renewal_at"] = expiry - timedelta(
                    days=self.object.spec.get("renewal_window_days", 30)
                )
            except (TypeError, ValueError):
                # A malformed provider timestamp must not break the resource page.
                pass
        context["operations"] = [
            operation_summary(operation) for operation in self.object.operations.all()[:20]
        ]
        for operation in context["operations"]:
            operation["created_at"] = datetime.fromisoformat(operation["created_at"])
            if operation["completed_at"]:
                operation["completed_at"] = datetime.fromisoformat(
                    operation["completed_at"]
                )
        context["resolved_spec"] = self.object.spec
        if self.object.kind == "tls.certificate":
            try:
                context["resolved_spec"] = controller_contract(self.object)["resource"]["spec"]
                context["topology_error"] = ""
                observed_names: dict[str, set[str]] = {}
                for observation in self.object.status.get("consumers", []):
                    observed_names.setdefault(observation.get("consumer", ""), set()).add(
                        observation.get("domain", "")
                    )
                context["display_consumers"] = [
                    {
                        **consumer,
                        "display_domains": sorted(
                            domain
                            for domain in observed_names.get(consumer["name"], set())
                            if domain
                        )
                        or consumer.get("verify_domains", []),
                    }
                    for consumer in context["resolved_spec"]["consumers"]
                ]
            except (KeyError, ValueError) as exc:
                context["topology_error"] = str(exc)
        context["diagnostic_status"] = serialize_public_status(self.object.status)
        return context


class ReconcileView(LoginRequiredMixin, View):
    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        try:
            result = _web_operation(request, resource, "reconcile")
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect("control_plane:detail", key=key)
        verb = "Queued" if result["queued"] else "Already queued"
        messages.success(request, f"{verb} reconciliation for “{resource.key}”.")
        return redirect("control_plane:detail", key=key)


class RenewCertificateView(LoginRequiredMixin, View):
    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        try:
            result = _web_operation(request, resource, "renew")
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect("control_plane:detail", key=key)
        verb = "Queued" if result["queued"] else "Already queued"
        messages.success(request, f"{verb} certificate renewal for “{resource.key}”.")
        return redirect("control_plane:detail", key=key)


class CertificateDownloadView(LoginRequiredMixin, View):
    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        certificate_pem = resource.status.get("certificate_pem", "")
        if resource.kind != "tls.certificate" or not certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "No verified public certificate is available."},
                status=404,
            )
        if "PRIVATE KEY-----" in certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "Unsafe certificate artifact rejected."},
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
