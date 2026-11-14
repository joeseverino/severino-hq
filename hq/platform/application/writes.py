"""Form views whose write is one capability, run through ``web_writes.run_posted``.

The write side of what ``TableListMixin`` is for the read side: a record's
create, update and delete share the same steps (its capability run under the
key its form posted, reload, announce, redirect), so they are declared once.
A view names its capabilities (``CapabilityFormMixin``,
``CapabilityDeleteMixin``), or for a plain ``Records`` domain only its form,
and the runner authorizes, audits and runs the write once per drawing of the
form.

A resend is answered from the first post's answer before the form is
validated again (``web_writes.posted_answer``), so that answer holds what a
replay stands on: the record as the capability answered it.

``ServiceCreateMixin``, ``ServiceUpdateMixin``, ``ServiceDeleteMixin`` and
``CommandFormMixin`` are the shapes ``hq_sdk.writes`` publishes, kept for one
``PLUGIN_API_VERSION``: each runs the capability its declared service is
registered as. Only a view whose service is no capability's, or which writes
in a ``run`` of its own, goes ``outside_the_runner``.
"""

from collections.abc import Callable, Mapping
from typing import Any, override

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.shortcuts import redirect
lazy from django.forms import BaseForm
lazy from django.http import HttpResponse

from hq.platform.application.routes import reverse

from .contracts import route_url
from .deletion import DeleteCommand
from .domains import load
from .integration_specs import command_adapter
from .labels import SAVE_CHANGES
from .record_errors import RecordConflict, RecordNotFound
from .records import described, display_noun, records_for
from .security import web_principal
from .ui import ended
from .web_writes import form_payload, posted_answer, refused, run_posted, write_once
lazy from .capabilities import Ran, capability_registry, capability_words
lazy from .domains import Records
lazy from .integrations import integration_graph
lazy from .security import Principal


def announce(request, template: str, noun: str, target: object) -> None:
    """Say a write landed: ``template`` names the record as ``{target}``, closed by ``ended``."""

    messages.success(request, ended(template.format(noun=noun, target=target)))


class WriteMessages:
    """What a write says when the view does not say it differently.

    A view rewords one by declaring it. A mixin that rewords one for several
    views subclasses this root, which then sits after it in any view's order
    of bases, so the mixin wins whatever order the bases were written in. Each
    names the record as ``{target}`` and what it is as ``{noun}``.
    """

    created_message = "{noun} “{target}” created."
    updated_message = "{noun} “{target}” updated."
    deleted_message = "{noun} “{target}” deleted."


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

    @property
    def _write(self) -> str:
        """What a write outside the runner is named by: this view's."""

        return f"{type(self).__module__}.{type(self).__qualname__}"

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


def outside_the_runner(request, write: str, operation: Callable[[], dict[str, Any]]) -> Ran:
    """A published form mixin's write that is no capability, run once under its posted key.

    The one way left around ``run_posted``, for an extension's view until it
    names a capability: its write is neither authorized nor held by the
    runner. No view of the host's reaches it (``test_view_writes``).
    """

    return write_once(request, write, operation)


def _registered(service: Callable[..., Any] | None, *, targeted: bool) -> str:
    """The capability ``service`` runs as, with or without a target, or "" for none."""

    return next(
        (
            spec.name
            for spec in capability_registry().values()
            if spec.handler is service and bool(spec.target_kind) == targeted
        ),
        "",
    )


def _command_payload(name: str, command: Any) -> dict[str, Any]:
    """A built command as capability ``name``'s payload, the JSON every door sends."""

    adapter = command_adapter(capability_registry()[name].command_type)
    return adapter.dump_python(adapter.validate_python(command), mode="json")


class _RecordWrite(PostedOnce, WriteMessages):
    """What a record's write declares: what it is called, and how an answer names it.

    ``result_key`` is where the answer nests the record, ``identity_attr`` the
    model field its capabilities target it by, and ``identity_result_key``
    that identity in the answer where it is named otherwise.
    """

    noun = ""
    result_key = ""
    identity_attr = ""
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

    def _went_to(self, answer: dict[str, Any], template: str) -> HttpResponse:
        self.object = self.reload(answer)
        announce(self.request, template, self.noun, self.object)
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

    def form_valid(self, form: BaseForm) -> HttpResponse:
        name = self.capability(form)
        payload = form_payload(form, capability_registry()[name].command_type)
        target = None if self.adding else self.stored_identity()
        return self._answered(form, run_posted(self.request, name, payload, target=target).answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        return self._went_to(answer, self.created_message if answer["created"] else self.updated_message)


class CapabilityDeleteMixin(_RecordWrite):
    """Delete a record through its capability (``deletes``), confirmed by its own identity.

    For ``DeleteView``. The label comes from the answer rather than from the
    object, because by the time there is something to announce the record is gone.
    """

    deletes = ""

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return _confirmed(self, super().get_context_data(**kwargs))

    def form_valid(self, form: BaseForm) -> HttpResponse:
        identity = self.stored_identity()
        ran = run_posted(self.request, self.deletes, {"confirm": str(identity)}, target=identity)
        return self._answered(form, ran.answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        announce(self.request, self.deleted_message, self.noun, answer["deleted"]["label"])
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
        "label": "Delete",
        "cancel_url": back() if back else view.success_url,
    }
    return context


def _refused_delete(view: Any, form: BaseForm, answer: dict[str, Any], refuse: Callable[..., HttpResponse]):
    # The confirmation page draws no form of its own to carry an error.
    refused(view.request, answer)
    return refuse(form, answer)


class _Records:
    """A plain record domain's capabilities, noun and identity, read off its ``Records`` declaration."""

    @property
    def records(self) -> Records:
        return records_for(self.get_form_class()._meta.model if self.model is None else self.model)

    @property
    def record_model(self) -> Any:
        return load(self.records.model)

    @property
    def noun(self) -> str:
        return display_noun(self.records)

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
    capabilities its declaration derives. ``created_message`` and
    ``updated_message`` may reword the announcement. Its help, its verb and
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
        listed = route_url(integration_graph().resources[records_for(form._meta.model).resource].web_route)
        return {**context, **form_close(self.object, self.get_form_verb(form), listed)}


class RecordDeleteMixin(_Records, CapabilityDeleteMixin):
    """Delete a plain record domain's record through its declared delete capability.

    For ``DeleteView``; ``deleted_message`` may reword the announcement.
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
        return described(super().get_form(form_class), capability_registry()[self.creates].command_type)

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


class ServiceWriteMixin(PostedOnce, WriteMessages):
    """Shared spine for a view whose mutation is one service call, run as the capability it is registered as.

    A subclass declares ``service`` (wrapped in ``staticmethod``), ``noun``
    (``"Content item"``), ``result_key`` (where the service nests its record),
    ``identity_attr`` (``"slug"`` or ``"pk"``) and, to amend, ``identity_kwarg``;
    ``identity_result_key`` names the identity in the result when it differs.
    """

    # No default for any of them: a placeholder collides with the real value on
    # whichever base supplies it, so base order picks the winner and reordering
    # breaks the view silently, and a missing noun reads as a blank in a message.
    # A slug default for identity_attr fails the same way, sending a record's
    # redirect to a slug it does not have. Read through getattr, so a subclass
    # that omits one is an error naming the attribute.
    REQUIRED = ("service", "noun", "result_key", "identity_attr")
    identity_result_key = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A plain function assigned to a class attribute becomes a bound method
        # on access, so ``self.service(command, ...)`` would silently pass the
        # view as the command. Caught at class creation, where the fix is
        # obvious, rather than at the first write, where it is not.
        super().__init_subclass__(**kwargs)
        for name in ("service", "command_from_cleaned_data"):
            value = cls.__dict__.get(name)
            if callable(value) and not isinstance(value, staticmethod):
                raise ImproperlyConfigured(f"{cls.__name__}.{name} must be wrapped in staticmethod().")
        # Only a class that supplies a service is a view rather than another
        # layer of mixin, so only that class has to be complete.
        if "service" not in cls.__dict__:
            return
        missing = [name for name in cls.REQUIRED if getattr(cls, name, None) in (None, "")]
        if missing:
            raise ImproperlyConfigured(f"{cls.__name__} writes through a service and declares no {', '.join(missing)}.")

    def write_principal(self) -> Principal:
        return web_principal(self.request.user)

    def current_identity(self) -> object:
        return getattr(self.get_object(), self.identity_attr)

    # The record is the subclass's own model, which this mixin cannot name.
    # Services return serializable results because the API and the MCP share
    # them, and a view needs a model instance for ``get_absolute_url`` and
    # ``__str__``: one query here is cheaper than the view rebuilding a URL the
    # model already knows.
    def reload(self, result: Mapping[str, Any]) -> Any:
        """The saved record, as a model instance the URL can be taken from."""

        key = self.identity_result_key or self.identity_attr
        return self.model._default_manager.get(**{self.identity_attr: result[self.result_key][key]})

    def announce(self, template: str, target: object) -> None:
        announce(self.request, template, self.noun, target)

    def _went_to(self, answer: dict[str, Any], template: str) -> HttpResponse:
        self.object = self.reload(answer)
        self.announce(template, self.object)
        return redirect(self.object.get_absolute_url())

    # Whether the service's capability takes a target: an update's and a delete's do.
    _targeted = False

    @classmethod
    def _capability(cls) -> str:
        """The capability the view's service is, or "" for one that is none, which runs ``outside_the_runner``."""

        return _registered(cls.service, targeted=cls._targeted)

    def _served(self, command: Any, identity: dict[str, Any] | None = None) -> dict[str, Any]:
        """The service's answer to ``command``: its capability's, or its own run ``outside_the_runner``."""

        name = self._capability()
        if name:
            target = next(iter(identity.values())) if identity else None
            return run_posted(self.request, name, _command_payload(name, command), target=target).answer
        principal = self.write_principal()
        return outside_the_runner(
            self.request, self._write, lambda: dict(self.service(command, principal=principal, **(identity or {})))
        ).answer


class ServiceCreateMixin(ServiceWriteMixin):
    """Create through a service, then go to what was created."""

    def form_valid(self, form: BaseForm) -> HttpResponse:
        return self._answered(form, self._served(self.command_from_cleaned_data(form.cleaned_data)))

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        return self._went_to(answer, self.created_message)


class ServiceUpdateMixin(ServiceWriteMixin):
    """Amend through a service, then go to where it ended up.

    Reloaded by the identity the service returned, not the one that arrived:
    a rename changes the slug, and redirecting to the old one 404s.
    """

    _targeted = True

    def form_valid(self, form: BaseForm) -> HttpResponse:
        command = self.command_from_cleaned_data(form.cleaned_data)
        return self._answered(form, self._served(command, {self.identity_kwarg: self.current_identity()}))

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        return self._went_to(answer, self.updated_message)


class ServiceDeleteMixin(ServiceWriteMixin):
    """Delete through a service, then go back to the list.

    The label comes from the result rather than from the object, because by the
    time there is something to announce the record is gone.
    """

    _targeted = True

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return _confirmed(self, super().get_context_data(**kwargs))

    def form_valid(self, form: BaseForm) -> HttpResponse:
        identity = self.current_identity()
        command = DeleteCommand(confirm=str(identity))
        return self._answered(form, self._served(command, {self.identity_kwarg: identity}))

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        self.announce(self.deleted_message, answer["deleted"]["label"])
        return redirect(self.success_url)

    @override
    def _refuse(self, form: BaseForm, answer: dict[str, Any]) -> HttpResponse:
        return _refused_delete(self, form, answer, super()._refuse)


class CommandFormMixin(PostedOnce):
    """A plain form whose submit is one service call: a *command* (adjust a
    threshold, open a period) posted from a page that then shows itself again,
    run as the capability the service is registered as.

    A domain's refusal becomes a form error beside its field rather than a 500.
    Subclasses declare ``service`` (as a ``staticmethod``), ``command`` (the
    command class, when the cleaned data maps straight onto one),
    ``success_url_name`` and ``success_message``.
    """

    service: Callable[..., Any] | None = None
    command: type[Any] | None = None
    success_url_name = ""
    success_message = ""
    # Which exceptions mean "the domain said no" rather than "HQ broke", for a
    # write outside the runner, which answers every other one itself. Kept
    # narrow and overridable: catching more than this turns a real fault into
    # a form error and hides it.
    domain_errors: tuple[type[Exception], ...] = (ValueError, RecordNotFound, RecordConflict)

    def build_command(self, form: BaseForm) -> Any:
        return self.command(**form.cleaned_data) if self.command else form.cleaned_data

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    def get_success_message(self, result: Any) -> str:
        return self.success_message

    def run(self, command: Any) -> Any:
        if self.service is None:
            raise ImproperlyConfigured(f"{type(self).__name__} declares no service.")
        return self.service(command, principal=web_principal(self.request.user))

    @classmethod
    def _capability(cls) -> str:
        """The capability the view's service is, or "" for a service that is none or a
        write of the view's own (a ``run`` that overrides), which runs ``outside_the_runner``."""

        return "" if cls.run is not CommandFormMixin.run else _registered(cls.service, targeted=False)

    def form_valid(self, form: BaseForm) -> HttpResponse:
        name = self._capability()
        if name:
            answer = run_posted(self.request, name, _command_payload(name, self.build_command(form))).answer
            return self._answered(form, answer)
        try:
            ran = outside_the_runner(
                self.request, self._write, lambda: {_SAID: self._said(self.run(self.build_command(form)))}
            )
        except self.domain_errors as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        return self._answered(form, ran.answer)

    def _said(self, result: Any) -> str:
        """What the write says, as text a resend can say again."""

        return str(self.get_success_message(result) or "")

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        said = self._said(answer) if self._capability() else answer[_SAID]
        if said:
            messages.success(self.request, said)
        return redirect(self.get_success_url())


# Where a write outside the runner keeps what it said, for a resend to say again.
_SAID = "said"


__all__ = [
    "CapabilityDeleteMixin",
    "CapabilityFormMixin",
    "CommandFormMixin",
    "RecordDeleteMixin",
    "RecordFormMixin",
    "ServiceCreateMixin",
    "ServiceDeleteMixin",
    "ServiceUpdateMixin",
    "ServiceWriteMixin",
    "WriteMessages",
    "outside_the_runner",
]
