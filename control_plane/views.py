from __future__ import annotations

import uuid
from datetime import datetime, timezone

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView, ListView

from application.infrastructure import (
    OperationCommand,
    PolicyError,
    certificate_renewal_allowed,
    request_certificate_renewal,
    request_reconcile,
    resource_health,
    serialize_resource,
)
from application.security import web_principal

from .models import ManagedResource, OperationRequest
from .providers import (
    controller_action_policy,
    describe_providers,
    validate_resolved_certificate,
)
from .topology import TopologyError, resolve_certificate


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
        not_after = self.object.status.get("not_after")
        if not_after:
            try:
                expiry = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                context["days_left"] = int(
                    (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
                )
            except (TypeError, ValueError):
                pass
        context["operations"] = self.object.operations.all()[:20]
        context["resolved_spec"] = self.object.spec
        if self.object.kind == "tls.certificate":
            try:
                context["resolved_spec"] = validate_resolved_certificate(
                    {
                        **resolve_certificate(self.object.spec["topology_ref"]),
                        "renewal_window_days": self.object.spec[
                            "renewal_window_days"
                        ],
                    }
                )
                context["topology_error"] = ""
            except (KeyError, TopologyError, ValueError) as exc:
                context["topology_error"] = str(exc)
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
        return JsonResponse(
            describe_providers(), json_dumps_params={"indent": 2}
        )
