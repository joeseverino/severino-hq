"""Human execution adapter for the canonical capability registry."""

from __future__ import annotations

import json
import secrets

from django.core.exceptions import PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views import View

from hq.platform.application.action_links import command_url
from hq.platform.application.capabilities import (
    authorize_capability,
    capability_registry,
    execute_capability,
)
from hq.platform.application.command_forms import command_form_class
from hq.platform.application.command_targets import (
    capability_target_initial,
    capability_target_options,
    with_requested_target,
)
from hq.platform.application.contracts import route_url
from hq.platform.application.idempotency import (
    IdempotencyConflict,
    execute_once,
    request_fingerprint,
    validate_key,
)
from hq.platform.application.integration_specs import capability_schema
from hq.platform.application.integrations import integration_graph
from hq.platform.application.pages import PageAction, page_context
from hq.platform.application.security import AuthorizationError, safe_next, web_principal
from hq.platform.application.ui import MISSING


def _json_value(value):
    """Normalize native form values to the same JSON contract as API/MCP."""

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _status(result: dict) -> int:
    return 200 if result.get("ok", False) else 400


_SCALAR = (str, int, float, bool, type(None))


def _cell(value) -> str:
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _result_projection(payload: dict) -> tuple[tuple[tuple[str, str], ...], dict | None]:
    """The facts a result states, and the one list it carries, if any.

    A result is a machine contract; a person reads it as facts. Most results are a handful of scalar facts and at most
    one list of flat records (resolver answers, matched rows) so those become
    a definition list and a table, and the JSON stays behind a disclosure for
    whoever is checking HQ against another tool.

    Generic on purpose. A handler describes its result by what it returns, not
    through presentation metadata, so nothing here knows what a key means. A
    shape these rules do not fit falls back to the JSON alone.
    """

    facts = tuple(
        (key.replace("_", " "), _cell(value))
        for key, value in payload.items()
        if key != "ok" and isinstance(value, _SCALAR)
    )
    for key, value in payload.items():
        if not isinstance(value, list) or not value:
            continue
        flat = all(
            isinstance(item, dict)
            and item
            and all(isinstance(field, _SCALAR) for field in item.values())
            for item in value
        )
        if not flat:
            continue
        columns = tuple(dict.fromkeys(column for item in value for column in item))
        return facts, {
            "label": key.replace("_", " "),
            "columns": tuple(column.replace("_", " ") for column in columns),
            "rows": tuple(
                tuple(_cell(item.get(column)) for column in columns) for item in value
            ),
        }
    return facts, None


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
        form.add_error(None, error.get("message", "That did not run."))


class CommandView(View):
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
        if self.target_options is not None:
            self.target_options = with_requested_target(
                self.spec,
                self.target_options,
                request.GET.get("target") or request.POST.get("__target", ""),
                principal=self.principal,
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
        resource = integration_graph().resources.get(self.spec.subject_resource)
        schema = capability_schema(self.spec)
        facts, table = _result_projection(result["payload"]) if result else ((), None)
        resource_url = route_url(resource.web_route) if resource else ""
        actions = [PageAction(f"Open {resource.label}", resource_url)] if resource_url else []
        return {
            **page_context(
                self.spec.title,
                self.spec.summary,
                actions=actions,
                trail=(("Search", reverse("search")),),
            ),
            "command": self.spec,
            "command_label": self.spec.title,
            "chosen_target": self._chosen_target(form),
            "form": form,
            "result": result,
            "result_facts": facts,
            "result_table": table,
            "result_json": (
                json.dumps(result["payload"], indent=2, sort_keys=True)
                if result
                else ""
            ),
            "return_url": safe_next(self.request),
            "resource_url": resource_url,
            "resource_label": resource.label if resource else "",
            "hydrates_target": bool(self.spec.target_initial_fields),
            # Only a command that writes a record's fields can blank them; one
            # that takes just a target and a reason has no record to replace.
            "writes_record": bool(
                set(schema.get("properties", {}))
                - {"idempotency_key", "reason"}
            ),
        }

    def _chosen_target(self, form):
        """The one this page was opened for, named instead of offered in a list.

        A link from a card or a record carries its target, and a list of every
        other choice beside it is noise. Only a target the list holds counts,
        so a link cannot name one the reader could not have picked.
        """

        asked = self.request.GET.get("target", "") if self.spec.target_kind else ""
        chosen = next(
            (option for option in self.target_options or () if option.value == asked), None
        )
        if chosen is None or form["__target"].value() != asked:
            return None
        field = form.fields["__target"]
        # Still the same control, so it posts as it would shown: only out of sight.
        field.widget.attrs["hidden"] = True
        return {
            "field": field.label,
            "label": chosen.label,
            "change_url": command_url(self.spec.name),
        }

    def get(self, request, name: str):
        initial = {"__execution_key": _new_key(), "next": safe_next(request)}
        target = request.GET.get("target", "") if self.spec.target_kind else ""
        known_targets = {option.value for option in self.target_options or ()}
        if target and (self.target_options is None or target in known_targets):
            initial["__target"] = target
            initial.update(
                capability_target_initial(self.spec, target, principal=self.principal)
            )
        form = self.form_class(initial=initial)
        context = self._context(form, result=self._result())
        # A link that named a target the form cannot offer says so, rather than
        # opening on an empty choice that looks like the link worked.
        context["unoffered_target"] = target if target and "__target" not in initial else ""
        return TemplateResponse(request, self.template_name, context)

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
        return redirect(command_url(self.spec.name, result=token, next=safe_next(request) or ""))
