"""Form views whose write is one capability, run through ``web_writes.run_posted``.

The write side of what ``TableListMixin`` is for the read side: a record's
create, update and delete share the same steps (its capability run under the
key its form posted, reload, say its answer's ``message``, redirect), so they
are declared once.
A view names its capabilities (``CapabilityFormMixin``,
``CapabilityDeleteMixin``), or for a plain ``Records`` domain only its form,
and the runner authorizes, audits and runs the write once per drawing of the
form.

A plain form that posts a command and shows its page again names it too
(``CapabilityCommandMixin``). None of them has a way to write but the runner.

A resend is answered from the first post's answer before the form is
validated again (``web_writes.posted_answer``), so that answer holds what a
replay stands on: the record as the capability answered it.
"""

from collections.abc import Callable, Mapping
from typing import Any, override

from django.http import Http404
from django.shortcuts import redirect
from django.views.generic.edit import BaseCreateView
lazy from django.forms import BaseForm
lazy from django.http import HttpResponse

from hq.platform.application.routes import reverse

from .contracts import route_url
from .domains import load
from .labels import SAVE_CHANGES
from .records import DELETE, described, records_for
from .web_writes import form_payload, posted_answer, refused, run_posted, said
lazy from .capabilities import capability_words, command_spec
lazy from .domains import Records
lazy from .integrations import held_graph


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
    """Create or update a record from its ``ModelForm`` through its capabilities.

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
    """What confirm_page.html needs: one button that posts here, and a way
    back to the record being deleted."""

    # Django leaves get_absolute_url for a model to define or not.
    back = getattr(view.object, "get_absolute_url", None)
    context["confirm"] = {
        "url": view.request.path,
        "label": capability_words(view.runs[0]) if view.runs else DELETE,
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
        return records_for(self.get_form_class()._meta.model if self.model is None else self.model)

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


class RecordFormMixin(_Records, CapabilityFormMixin):
    """Create or update a plain record domain's record from its ``ModelForm``.

    For ``CreateView`` and ``UpdateView``, through the create and update
    capabilities its declaration derives. Its help, its verb and
    its way back come from the declaration too, so it reads as the command's
    page does, and its page is the form page frame.
    """

    template_name = "form_page.html"

    @property
    def creates(self) -> str:
        return f"{self.records.noun}.create"

    @property
    def updates(self) -> str:
        return f"{self.records.noun}.update"

    def get_form(self, form_class: Any = None) -> Any:
        form = super().get_form(form_class)
        return described(form, load(records_for(form._meta.model).command))

    def get_form_verb(self, form: Any) -> str:
        """What makes a new one: its create command's words."""

        return capability_words(f"{records_for(form._meta.model).noun}.create")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = context["form"]
        listed = route_url(held_graph().resources[records_for(form._meta.model).resource].web_route)
        return {**context, **form_close(self.object, self.get_form_verb(form), listed)}


class RecordDeleteMixin(_Records, CapabilityDeleteMixin):
    """Delete a plain record domain's record through its declared delete capability.

    For ``DeleteView``.
    """

    @property
    def deletes(self) -> str:
        return f"{self.records.noun}.delete"


class CommandFormPage:
    """A record's form whose create goes through a named command (``creates``)
    rather than its domain's declaration: its help is that command's, and it
    closes with its words, or Save changes, and Cancel (``listed_route`` for a
    new one), on the form page frame."""

    template_name = "form_page.html"
    creates = ""
    listed_route = ""

    def get_form(self, form_class: Any = None) -> Any:
        return described(super().get_form(form_class), command_spec(self.creates).command_type)

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
    """A plain form whose submit runs a capability (``submits``), from a
    page that then shows itself again (``success_url_name``).

    For ``FormView``. The form's values are sent as the command's payload
    (``payload``, ``form_payload`` unless a view shapes them), on the record
    ``target`` names, and a refusal's field problems land beside their
    fields. A form whose controls run several commands names the one a post
    runs (``capability``) and declares them all as ``runs``.
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
