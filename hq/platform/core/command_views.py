"""Human execution adapter for the canonical capability registry."""

import json
import secrets
from typing import Any, override

from django import forms
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
)
from hq.platform.application.command_forms import command_form_class, own_fields
from hq.platform.application.command_targets import (
    capability_target_initial,
    capability_target_options,
    page_named,
    record_options,
    starting_page,
    with_requested_target,
)
from hq.platform.application.contracts import route_url
from hq.platform.application.integration_specs import ON_ITS_PAGE, capability_schema, kept_by
from hq.platform.application.integrations import integration_graph
from hq.platform.application.pages import page_context
from hq.platform.application.payload_words import value_words
from hq.platform.application.refusal_status import refusal_of
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.service_context import Cell, ServiceSection
from hq.platform.application.ui import MISSING
from hq.platform.application.web_writes import refused, run_posted


def _json_value(value):
    """Normalize native form values to the same JSON contract as API/MCP."""

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


_SCALAR = (str, int, float, bool, type(None))


def _cell(value) -> str:
    return MISSING if value is None else value_words(value)


def _result_projection(payload: dict) -> tuple[tuple[tuple[str, str], ...], ServiceSection | None]:
    """The facts a result states, and the one list it carries, if any.

    A result is a machine contract that a person reads as facts: its scalars
    become a definition list and its one list of flat records a table, the JSON
    kept behind a disclosure. A handler describes a result only by what it
    returns, so nothing here knows what a key means, and a shape that does not
    fit shows the JSON alone.
    """

    facts = tuple(
        (key.replace("_", " "), _cell(value))
        for key, value in payload.items()
        if key != "ok" and isinstance(value, _SCALAR)
    )
    listed = next(((key, value) for key, value in payload.items() if _is_flat_records(value)), None)
    return facts, _result_table(*listed) if listed else None


# What a command that queues work answers beside it: whether this call queued
# it, which the work's own row says as its state.
_QUEUED_WORK = frozenset({"ok", "queued", "operation"})


def _only_queues(payload: dict) -> bool:
    """Whether a result says only that work was queued, so the page draws no
    result: the work stands in its own row, among its runs or on Jobs."""

    return "operation" in payload and set(payload) <= _QUEUED_WORK


def _is_flat_records(value: object) -> bool:
    """Whether a value is a list of records, each a non-empty mapping of scalars."""

    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict) and item and all(isinstance(field, _SCALAR) for field in item.values())
            for item in value
        )
    )


def _result_table(key: str, records: list[dict]) -> ServiceSection:
    """A list of records as a table: a column for every field any record has,
    drawn as every registry-driven table is (partials/_page_sections.html)."""

    columns = tuple(dict.fromkeys(column for item in records for column in item))
    return ServiceSection(
        id="command-result-list",
        label=key.replace("_", " ").strip() or "records",
        columns=tuple(column.replace("_", " ") for column in columns),
        records=tuple(tuple(Cell(_cell(item.get(column))) for column in columns) for item in records),
    )


def _named_records(form, query) -> dict[str, Any]:
    """The records a link names for each field that chooses records (a run's
    machines), kept only where the field offers them, so a link cannot
    choose one its reader could not have picked. A form's own fields
    (``__target``, its confirmation) and any other field are never filled
    from a link."""

    named: dict[str, Any] = {}
    for name, field in form.fields.items():
        if name.startswith("__") or not isinstance(field, forms.TypedChoiceField | forms.TypedMultipleChoiceField):
            continue
        offered = [value for value in query.getlist(name) if value and field.valid_value(value)]
        if offered:
            named[name] = offered if isinstance(field, forms.TypedMultipleChoiceField) else offered[0]
    return named


class CommandView(View):
    """Render and execute any permitted capability without reimplementing it."""

    template_name = "command.html"

    @override
    def dispatch(self, request, name: str, *args, **kwargs):
        self.spec = capability_registry().get(name)
        if self.spec is None:
            raise Http404("Unknown command.")
        self.principal = web_principal(request.user)
        try:
            authorize_capability(self.spec, self.principal)
        except Exception as exc:
            raise PermissionDenied(refusal_of(exc, name=self.spec.name, principal=self.principal).message) from exc
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
            self.spec,
            target_options=self.target_options,
            record_choices=record_options(capability_schema(self.spec), principal=self.principal),
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
        if result and _only_queues(result["payload"]):
            result = None
        resource = integration_graph().resources.get(self.spec.subject_resource)
        schema = capability_schema(self.spec)
        facts, table = _result_projection(result["payload"]) if result else ((), None)
        resource_url = route_url(resource.web_route) if resource else ""
        # The trail names where the form lives: the records it writes, the
        # page it declares as its home, or Search for a command that has neither.
        if resource_url:
            trail = ((resource.label, resource_url),)
        elif self.spec.home:
            trail = (page_named(self.spec.home),)
        else:
            trail = (("Search", reverse("search")),)
        return {
            # No lede: the summary is the agent's description of the command,
            # and the page says what it changes in its fields and its button.
            **page_context(self.spec.title, trail=trail),
            "command": self.spec,
            "command_label": self.spec.title,
            "chosen_target": self._chosen_target(form),
            "form": form,
            "result": result,
            "result_facts": facts,
            # The one list it carries, as the sections partials/_page_sections.html draws.
            "result_sections": (table,) if table else (),
            "result_json": (json.dumps(result["payload"], indent=2, sort_keys=True) if result else ""),
            # The JSON opens only when the result shows nothing else.
            "result_json_open": not facts and table is None,
            "return_url": safe_next(self.request),
            # Cancel goes back where it was opened from, else to the trail's end.
            "cancel_url": safe_next(self.request) or trail[-1][1],
            "resource_url": resource_url,
            "resource_label": resource.label if resource else "",
            "hydrates_target": bool(self.spec.target_initial_fields),
            "switch": self._switch(form),
            # A command that needs what one page hands it says to start there.
            "starting_page": starting_page(self.spec),
            "starts_on_its_page": self.spec.reached_from == ON_ITS_PAGE,
            "saves_blanks": self._saves_blanks(schema),
            # What the command says of itself beside its form, if it says anything.
            "about": self.spec.about(web_principal(self.request.user)) if self.spec.about is not None else None,
        }

    def _saves_blanks(self, schema: dict) -> bool:
        """Whether a field left blank here overwrites what the record holds.

        The form leaves a blank field out, so a command on a record that may
        be sent without it saves its default. One that keeps what it is not
        sent (``Kept``), a form that opens holding the record's values, or a
        command whose every field is asked for, blanks nothing.
        """

        if not self.spec.target_kind or self.spec.target_initial_fields or kept_by(self.spec):
            return False
        return bool(own_fields(schema) - set(schema.get("required", ())))

    def _switch(self, form):
        """A command that sets one thing to yes or no, as its two buttons.

        On a page opened for one record whose value the form holds, or for a
        command that declares its ``state``, it says that value and offers
        only the button that changes it.
        """

        field = form.switch
        if not field:
            return None
        # Present where a chosen record was read into the form, else read
        # from the command's own declaration of where it stands.
        held = form.initial.get(field)
        if held is None and self.spec.state is not None:
            if not self.spec.target_kind:
                held = self.spec.state()
            elif target := form["__target"].value():
                held = self.spec.state(str(target))
        state = None if held is None else bool(held)
        choices = tuple(
            ("true" if value else "false", self.spec.words(variant))
            for variant, value in (("on", True), ("off", False))
            if state is not value
        )
        return {
            "field": field,
            "label": self.spec.title,
            "state": "" if state is None else ("On" if state else "Off"),
            "choices": choices,
        }

    def _chosen_target(self, form):
        """The one this page was opened for, named instead of offered in a list.

        A link from a card or a record carries its target, and a list of every
        other choice beside it is noise. Only a target the list holds counts,
        so a link cannot name one the reader could not have picked.
        """

        asked = self.request.GET.get("target", "") if self.spec.target_kind else ""
        chosen = next((option for option in self.target_options or () if option.value == asked), None)
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
        initial = {"next": safe_next(request)}
        target = request.GET.get("target", "") if self.spec.target_kind else ""
        known_targets = {option.value for option in self.target_options or ()}
        if target and (self.target_options is None or target in known_targets):
            initial["__target"] = target
            initial.update(capability_target_initial(self.spec, target, principal=self.principal))
        form = self.form_class(initial=initial)
        if named := _named_records(form, request.GET):
            form = self.form_class(initial={**initial, **named})
        context = self._context(form, result=self._result())
        # A link that named a target the form cannot offer says so, rather than
        # opening on an empty choice that looks like the link worked.
        context["unoffered_target"] = target if target and "__target" not in initial else ""
        return TemplateResponse(request, self.template_name, context)

    def _held(self, target: str) -> dict:
        """What the record a form edits holds, so a posted value its source no
        longer lists is still the one it keeps (``CapabilityCommandForm._hold``)."""

        known = {option.value for option in self.target_options or ()}
        if not target or (self.target_options is not None and target not in known):
            return {}
        return capability_target_initial(self.spec, target, principal=self.principal)

    def post(self, request, name: str):
        form = self.form_class(request.POST, request.FILES, initial=self._held(request.POST.get("__target", "")))
        if not form.is_valid():
            return TemplateResponse(request, self.template_name, self._context(form), status=400)

        payload = _json_value(form.command_payload)
        target = form.cleaned_data.get("__target")
        expected_updated_at = form.cleaned_data.get("__expected_updated_at") or None
        result, replayed = run_posted(
            request,
            self.spec.name,
            payload,
            target=target,
            expected_updated_at=expected_updated_at,
        )
        if refusal := refused(request, result, form=form):
            return TemplateResponse(request, self.template_name, self._context(form), status=refusal.status)

        token = secrets.token_urlsafe(18)
        request.session["command_center_result"] = {
            "token": token,
            "command": self.spec.name,
            "payload": result,
            "replayed": replayed,
        }
        return redirect(command_url(self.spec.name, result=token, next=safe_next(request) or ""))
