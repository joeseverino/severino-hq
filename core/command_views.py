"""Human execution adapter for the canonical capability registry."""

from __future__ import annotations

import json
import secrets

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views import View

from application.capabilities import (
    authorize_capability,
    capability_label,
    capability_registry,
    command_schema,
    execute_capability,
)
from application.command_forms import command_form_class
from application.command_targets import (
    capability_target_initial,
    capability_target_options,
)
from application.contracts import route_url
from application.idempotency import (
    IdempotencyConflict,
    execute_once,
    request_fingerprint,
    validate_key,
)
from application.resources import resource_specs
from application.security import AuthorizationError, web_principal


def _json_value(value):
    """Normalize native form values to the same JSON contract as API/MCP."""

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _status(result: dict) -> int:
    return 200 if result.get("ok", False) else 400


def _new_key() -> str:
    return validate_key(f"web:{secrets.token_urlsafe(24)}")


def _renew_key(form) -> None:
    data = form.data.copy()
    data["__execution_key"] = _new_key()
    form.data = data


def _apply_execution_error(form, result: dict) -> None:
    error = result.get("error", {})
    details = error.get("details")
    placed = False
    if isinstance(details, list):
        for item in details:
            location = item.get("loc", ()) if isinstance(item, dict) else ()
            field = str(location[0]) if location else ""
            message = (
                item.get("msg", "Invalid value.") if isinstance(item, dict) else ""
            )
            form.add_error(field if field in form.fields else None, message)
            placed = True
    if not placed:
        form.add_error(None, error.get("message", "The command could not be executed."))


class CommandView(LoginRequiredMixin, View):
    """Render and execute any permitted capability without reimplementing it."""

    template_name = "command.html"

    def dispatch(self, request, name: str, *args, **kwargs):
        self.spec = capability_registry().get(name)
        if self.spec is None:
            raise Http404("Unknown command.")
        self.principal = web_principal(request.user)
        try:
            authorize_capability(self.spec, self.principal)
        except AuthorizationError as exc:
            raise PermissionDenied(str(exc)) from exc
        self.target_options = capability_target_options(
            self.spec,
            principal=self.principal,
            governed_kinds=tuple(dict.fromkeys(request.GET.getlist("kind"))),
        )
        self.form_class = command_form_class(
            self.spec, target_options=self.target_options
        )
        return super().dispatch(request, name, *args, **kwargs)

    def _result(self):
        saved = self.request.session.get("command_center_result")
        token = self.request.GET.get("result", "")
        if not saved or not token or saved.get("token") != token:
            return None
        if saved.get("command") != self.spec.name:
            return None
        return saved

    def _context(self, form, *, result=None):
        resources = {spec.name: spec for spec in resource_specs()}
        resource = resources.get(self.spec.subject_resource)
        schema = command_schema(self.spec.command_type)
        required_capabilities = tuple(
            item.value if hasattr(item, "value") else str(item)
            for item in self.spec.required_capabilities
        )
        return {
            "command": self.spec,
            "command_label": capability_label(self.spec.name),
            "effect_label": self.spec.effect.replace("_", " "),
            "form": form,
            "result": result,
            "result_json": (
                json.dumps(result["payload"], indent=2, sort_keys=True)
                if result
                else ""
            ),
            "schema_json": json.dumps(schema, indent=2, sort_keys=True),
            "resource_url": route_url(resource.web_route) if resource else "",
            "resource_label": resource.label if resource else "",
            "handler_name": self.spec.handler.__name__,
            "required_capabilities": required_capabilities,
            "target_catalog_count": (
                len(self.target_options) if self.target_options is not None else None
            ),
            "has_reason": "reason" in form.fields,
            "hydrates_target": bool(self.spec.target_initial_fields),
            "effect_outcome": {
                "read": "Reads authorized state without changing it.",
                "remote_write": "Commits an atomic change to HQ state.",
                "infrastructure_change": (
                    "Queues or performs a policy-gated external change."
                ),
                "destructive": "Removes or retires the selected state.",
            }.get(self.spec.effect, self.spec.effect.replace("_", " ")),
        }

    def get(self, request, name: str):
        initial = {"__execution_key": _new_key()}
        target = request.GET.get("target", "") if self.spec.target_kind else ""
        known_targets = {option.value for option in self.target_options or ()}
        if target and (self.target_options is None or target in known_targets):
            initial["__target"] = target
            initial.update(
                capability_target_initial(self.spec, target, principal=self.principal)
            )
        form = self.form_class(initial=initial)
        return TemplateResponse(
            request, self.template_name, self._context(form, result=self._result())
        )

    def post(self, request, name: str):
        form = self.form_class(request.POST)
        if not form.is_valid():
            return TemplateResponse(
                request, self.template_name, self._context(form), status=400
            )

        payload = _json_value(form.command_payload)
        target = form.cleaned_data.get("__target")
        expected_updated_at = form.cleaned_data.get("__expected_updated_at") or None
        envelope = {
            "command": payload,
            "target": target,
            "expected_updated_at": expected_updated_at,
        }

        def run():
            result = execute_capability(
                self.spec.name,
                payload,
                principal=self.principal,
                target=target,
                expected_updated_at=expected_updated_at,
            )
            return result, _status(result)

        try:
            if self.spec.effect == "read":
                result, status = run()
                replayed = False
            else:
                result, status, replayed = execute_once(
                    actor=self.principal.actor,
                    key=validate_key(form.cleaned_data["__execution_key"]),
                    request_sha256=request_fingerprint(
                        self.spec.name, envelope, api_version=2
                    ),
                    operation=run,
                )
        except IdempotencyConflict as exc:
            form.add_error(None, exc.reason)
            _renew_key(form)
            return TemplateResponse(
                request, self.template_name, self._context(form), status=409
            )

        if not result.get("ok", False):
            _apply_execution_error(form, result)
            _renew_key(form)
            return TemplateResponse(
                request, self.template_name, self._context(form), status=status
            )

        token = secrets.token_urlsafe(18)
        request.session["command_center_result"] = {
            "token": token,
            "command": self.spec.name,
            "payload": result,
            "replayed": replayed,
        }
        destination = reverse("command", kwargs={"name": self.spec.name})
        return redirect(f"{destination}?result={token}")
