"""Form views whose write is one capability, run through ``web_writes.run_posted``.

The write side of what ``ListPageMixin`` is for the read side: a record's
create, update and delete share their steps, so they are declared once, and
none has a way to write but the runner. A resend is answered from the first
post's answer (``web_writes.posted_answer``).
"""

from collections.abc import Callable, Mapping
from dataclasses import fields
from typing import Any, override

from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.shortcuts import redirect
from django.views.generic.edit import BaseCreateView
lazy from django.forms import BaseForm
lazy from django.http import HttpResponse

from hq.platform.application.routes import reverse
from hq.platform.core.web_refusals import refused

from .contracts import route_url
from .host_sections import load
from .labels import SAVE_CHANGES
from .records import records_for
from .web_writes import form_payload, posted_answer, run_posted, said
lazy from .capabilities import capability_words, command_spec
lazy from .command_fields import for_agents
lazy from .command_forms import command_form_class
lazy from .command_targets import record_options
lazy from .integration_specs import capability_schema, kept_by
lazy from .integrations import held_graph
lazy from .pages import PageAction
lazy from .record_declaration import Records
lazy from .references import ReferenceField
lazy from .security import web_principal


class PostedOnce:
    """A form view whose write acts once per drawing of its form.

    A resend the form still takes runs again under its key, which answers it
    as the first post was answered, or refuses it as a different request. One
    the first post made impossible (a slug now taken, a record now gone) is
    answered from that first answer instead of refused.
    """

    def post(self, request, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            return super().post(request, *args, **kwargs)
        except Http404:
            answer = posted_answer(request)
            if answer is None:
                raise
            return self._replay(answer)

    def form_invalid(self, form: BaseForm) -> HttpResponse:
        if not getattr(self, "_refusing", False) and (answer := posted_answer(self.request)) is not None:
            return self._replay(answer)
        return super().form_invalid(form)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        """Where the write's ``answer`` goes, said again: the first post's or a resend's."""

        raise NotImplementedError

    def _refuse(self, form: BaseForm, answer: dict[str, Any]) -> HttpResponse:
        """The form again, carrying why the write was refused, with the refusal's status."""

        refusal = refused(self.request, answer, form=form)
        self._refusing = True
        response = self.form_invalid(form)
        response.status_code = refusal.status
        return response

    def _answered(self, form: BaseForm, answer: dict[str, Any]) -> HttpResponse:
        return self._replay(answer) if answer["ok"] else self._refuse(form, answer)


class _RecordWrite(PostedOnce):
    """What a record's write declares: how an answer names it.

    ``result_key`` is where the answer nests the record, ``identity_attr`` the
    model field its capabilities target it by, and ``identity_result_key``
    that identity in the answer where it is named otherwise. Each write
    declares the first two, as a value or a property, so no base holds a
    value of them another base contradicts.
    """

    result_key: str
    identity_attr: str
    identity_result_key = ""

    @property
    def record_model(self) -> Any:
        return self.model

    def stored_identity(self) -> Any:
        """The record's identity as stored, which a form being saved may already have
        changed, as a target is sent: a number, or text (a UUID's)."""

        identity = getattr(self.get_object(), self.identity_attr)
        return identity if isinstance(identity, int) else str(identity)

    def reload(self, answer: Mapping[str, Any]) -> Any:
        """The saved record, as a model instance the URL can be taken from."""

        key = self.identity_result_key or self.identity_attr
        return self.record_model._default_manager.get(**{self.identity_attr: answer[self.result_key][key]})

    def _went_to(self, answer: dict[str, Any]) -> HttpResponse:
        self.object = self.reload(answer)
        said(self.request, answer)
        return redirect(self.object.get_absolute_url())


class CapabilityFormMixin(_RecordWrite):
    """Create or update a record from its command's form through its capabilities.

    For ``CreateView`` and ``UpdateView``: ``creates`` and ``updates`` name
    them, the update targeted at the record as stored. The form's values are
    sent as the command's payload (``form_payload``), and a refusal's field
    problems land beside their fields.
    """

    creates = ""
    updates = ""

    def capability(self, form: BaseForm) -> str:
        """The capability this post runs."""

        return self.creates if self.adding else self.updates

    @property
    def adding(self) -> bool:
        return getattr(self, "object", None) is None

    @property
    def runs(self) -> tuple[str, ...]:
        """The command a control for it opens this form to run (``pages.command_route``):
        a create view's create, an update view's update."""

        return (self.creates if isinstance(self, BaseCreateView) else self.updates,)

    def form_valid(self, form: BaseForm) -> HttpResponse:
        name = self.capability(form)
        payload = form_payload(form, command_spec(name).command_type)
        target = None if self.adding else self.stored_identity()
        return self._answered(form, run_posted(self.request, name, payload, target=target).answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        return self._went_to(answer)


class CapabilityDeleteMixin(_RecordWrite):
    """Delete a record through its capability (``deletes``), confirmed by its own identity.

    For ``DeleteView``.
    """

    deletes = ""

    @property
    def runs(self) -> tuple[str, ...]:
        return (self.deletes,)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return _confirmed(self, super().get_context_data(**kwargs))

    def form_valid(self, form: BaseForm) -> HttpResponse:
        identity = self.stored_identity()
        ran = run_posted(self.request, self.deletes, {"confirm": str(identity)}, target=identity)
        return self._answered(form, ran.answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        said(self.request, answer)
        return redirect(self.success_url)

    @override
    def _refuse(self, form: BaseForm, answer: dict[str, Any]) -> HttpResponse:
        return _refused_delete(self, form, answer, super()._refuse)


def _confirmed(view: Any, context: dict[str, Any]) -> dict[str, Any]:
    """What confirm_page.html needs: the press that deletes the record, for
    whoever may, and a way back to it."""

    # Django leaves get_absolute_url for a model to define or not.
    back = getattr(view.object, "get_absolute_url", None)
    context["confirm"] = {
        "action": PageAction.post(view.deletes, dict(view.kwargs), confirms=True),
        "cancel_url": back() if back else view.success_url,
    }
    return context


def _refused_delete(view: Any, form: BaseForm, answer: dict[str, Any], refuse: Callable[..., HttpResponse]):
    # The confirmation page draws no form of its own to carry an error.
    refused(view.request, answer)
    return refuse(form, answer)


class _Records(_RecordWrite):
    """A plain record domain's capabilities and identity, read off its ``Records`` declaration.

    It is a record write itself, so its model and identity override the
    defaults the write mixins it is composed with inherit.
    """

    @property
    def records(self) -> Records:
        return records_for(self.model)

    @property
    def record_model(self) -> Any:
        return load(self.records.model)

    @property
    def result_key(self) -> str:
        return self.records.noun

    @property
    def identity_attr(self) -> str:
        return self.records.lookup

    @property
    def identity_result_key(self) -> str:
        return "id" if self.records.lookup == "pk" else ""


class CommandForm:
    """A record's form, derived from the command its create sends, so each
    field says what that command says of it at every door.

    For ``CreateView`` and ``UpdateView`` beside ``CapabilityFormMixin``.
    ``field_order`` orders the fields where the command's order is not a
    person's.
    """

    template_name = "form_page.html"
    field_order: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "form_class" in vars(cls):
            raise ImproperlyConfigured(f"{cls.__name__} names a form; a record's form is built from its command.")

    def get_form_class(self) -> Any:
        spec = command_spec(self.creates)
        drawn = self.field_order or tuple(
            item.name for item in fields(spec.command_type) if item.name not in for_agents(spec.command_type)
        )
        offered = record_options(capability_schema(spec), principal=web_principal(self.request.user))
        return command_form_class(spec, fields=drawn, record_choices=offered)

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs.pop("instance", None)
        if not self.adding:
            kept = kept_by(command_spec(self.updates))
            if kept is None:
                raise ImproperlyConfigured(f"{self.updates} keeps nothing of the record, so its form opens empty.")
            kwargs["initial"] = {**kept.read(self.stored_identity()), **kwargs["initial"]}
            kwargs["held_names"] = {
                column.name: getattr(self.object, column.name_attname)
                for column in type(self.object)._meta.get_fields()
                if isinstance(column, ReferenceField)
            }
        return kwargs


class RecordFormMixin(CommandForm, _Records, CapabilityFormMixin):
    """Create or update a plain record domain's record through the create and
    update capabilities its declaration derives. Its help, its verb and its
    way back come from the declaration too, so it reads as the command's page
    does, and its page is the form page frame. A view names its ``model``.
    """

    @property
    def creates(self) -> str:
        return f"{self.records.noun}.create"

    @property
    def updates(self) -> str:
        return f"{self.records.noun}.update"

    def get_form_verb(self, form: Any) -> str:
        """What makes a new one: its create command's words."""

        return capability_words(self.creates)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        listed = route_url(held_graph().resources[self.records.resource].web_route)
        return {**context, **form_close(self.object, self.get_form_verb(context["form"]), listed)}


class RecordDeleteMixin(_Records, CapabilityDeleteMixin):
    """Delete a plain record domain's record through its declared delete capability.

    For ``DeleteView``.
    """

    @property
    def deletes(self) -> str:
        return f"{self.records.noun}.delete"


class CommandFormPage(CommandForm):
    """A record's form whose create goes through a named command (``creates``)
    rather than its domain's declaration: it closes with that command's
    words, or Save changes, and Cancel (``listed_route`` for a new one), on
    the form page frame.
    """

    creates = ""
    listed_route = ""

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        adds = capability_words(self.creates)
        return {**context, **form_close(self.object, adds, reverse(self.listed_route))}


def form_close(stored: Any, adds: str, listed: str) -> dict[str, str]:
    """What a record's form closes with (partials/_form_actions.html): ``adds``
    and Cancel to ``listed`` for a new one, Save changes and Cancel back to
    ``stored``, the record as stored, which a refused edit's form does not hold."""

    if stored is None:
        return {"form_verb": adds, "cancel_url": listed}
    return {"form_verb": SAVE_CHANGES, "cancel_url": stored.get_absolute_url()}


class CapabilityCommandMixin(PostedOnce):
    """A plain form whose submit runs a capability (``submits``), from a page
    that then shows itself again (``success_url_name``).

    For ``FormView``. The form's values are the command's ``payload`` on the
    record ``target`` names. A form running several commands names the one a
    post runs (``capability``) and declares them all as ``runs``.
    """

    submits = ""
    success_url_name = ""

    @property
    def runs(self) -> tuple[str, ...]:
        return (self.submits,)

    def capability(self, form: BaseForm) -> str:
        """The capability this post runs."""

        return self.submits

    def target(self) -> str | int | None:
        """The record it runs on, or None for a command that takes none."""

        return None

    def payload(self, form: BaseForm) -> dict[str, Any]:
        """What the form sends its capability: its values as the command takes them."""

        return form_payload(form, command_spec(self.capability(form)).command_type)

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    def form_valid(self, form: BaseForm) -> HttpResponse:
        ran = run_posted(self.request, self.capability(form), self.payload(form), target=self.target())
        return self._answered(form, ran.answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        said(self.request, answer)
        return redirect(self.get_success_url())


__all__ = [
    "CapabilityCommandMixin",
    "CapabilityDeleteMixin",
    "CapabilityFormMixin",
    "RecordDeleteMixin",
    "RecordFormMixin",
]
