"""Form views whose write is one application service call.

The write side of what ``TableListMixin`` is for the read side: create, update
and delete share the same steps (command, service call, reload, announce,
redirect), so they are declared once and a plugin adding a domain inherits
them. A view declares only which service, which record and what it is called;
a plain ``Records`` domain's view names only its form.

Each writes once per drawing of its form (``web_writes.write_once``), and
answers a resend with what the first post's answer kept, so that answer holds
everything a replay stands on: the service's result, which ``reload`` is
handed either way, or the record's identity, a label, the sentence it said.
"""

from collections.abc import Callable, Mapping
from typing import Any, override

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.shortcuts import redirect
lazy from django.forms import BaseForm
lazy from django.http import HttpResponse

from hq.platform.application.routes import reverse

from .deletion import DeleteCommand
from .record_errors import RecordConflict, RecordNotFound
from .records import delete_instance, display_noun, records_for, save_form
from .refusal_status import refusal_of
from .security import web_principal
from .ui import ended
from .web_writes import posted_answer, refused, write_once
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

    A resend of a post that already wrote is answered from that first answer
    before the form is validated again, since what the first post wrote can
    make its own resend invalid (a slug now taken, a record now gone).
    """

    def post(self, request, *args: Any, **kwargs: Any) -> HttpResponse:
        answer = posted_answer(request, self._write)
        if answer is not None:
            return self._replay(answer)
        return super().post(request, *args, **kwargs)

    @property
    def _write(self) -> str:
        """What the key names: this view's write, wherever its service or ``run`` is declared."""

        return f"{type(self).__module__}.{type(self).__qualname__}"

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        """Where the write's ``answer`` goes, said again: the first post's or a resend's."""

        raise NotImplementedError

    def _refuse(self, form: BaseForm, answer: dict[str, Any]) -> HttpResponse:
        """The form again, carrying why the write was refused, with the refusal's status."""

        refused(self.request, answer, form=form)
        response = self.form_invalid(form)
        response.status_code = refusal_of(answer).status
        return response

    def _answered(self, form: BaseForm, answer: dict[str, Any]) -> HttpResponse:
        return self._replay(answer) if answer["ok"] else self._refuse(form, answer)


class ServiceWriteMixin(PostedOnce, WriteMessages):
    """Shared spine for a view whose mutation is one service call.

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
        self.object = self.reload(answer["record"])
        self.announce(template, self.object)
        return redirect(self.object.get_absolute_url())


class ServiceCreateMixin(ServiceWriteMixin):
    """Create through a service, then go to what was created."""

    def form_valid(self, form: BaseForm) -> HttpResponse:
        command = self.command_from_cleaned_data(form.cleaned_data)
        ran = write_once(
            self.request, self._write, lambda: {"record": self.service(command, principal=self.write_principal())}
        )
        return self._answered(form, ran.answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        return self._went_to(answer, self.created_message)


class ServiceUpdateMixin(ServiceWriteMixin):
    """Amend through a service, then go to where it ended up.

    Reloaded by the identity the service returned, not the one that arrived:
    a rename changes the slug, and redirecting to the old one 404s.
    """

    def form_valid(self, form: BaseForm) -> HttpResponse:
        command = self.command_from_cleaned_data(form.cleaned_data)
        identity = {self.identity_kwarg: self.current_identity()}
        ran = write_once(
            self.request,
            self._write,
            lambda: {"record": self.service(command, principal=self.write_principal(), **identity)},
        )
        return self._answered(form, ran.answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        return self._went_to(answer, self.updated_message)


class ServiceDeleteMixin(ServiceWriteMixin):
    """Delete through a service, then go back to the list.

    The label comes from the result rather than from the object, because by the
    time there is something to announce the record is gone.
    """

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        # What confirm_page.html needs: one button that posts here, and a way
        # back to the record being deleted.
        context = super().get_context_data(**kwargs)
        # Django leaves get_absolute_url for a model to define or not.
        back = getattr(self.object, "get_absolute_url", None)
        context["confirm"] = {
            "url": self.request.path,
            "label": "Delete",
            "cancel_url": back() if back else self.success_url,
        }
        return context

    def form_valid(self, form: BaseForm) -> HttpResponse:
        identity = self.current_identity()
        ran = write_once(
            self.request,
            self._write,
            lambda: _deleted(
                self.service(
                    DeleteCommand(confirm=str(identity)),
                    principal=self.write_principal(),
                    **{self.identity_kwarg: identity},
                )
            ),
        )
        return self._answered(form, ran.answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        self.announce(self.deleted_message, answer["label"])
        return redirect(self.success_url)

    @override
    def _refuse(self, form: BaseForm, answer: dict[str, Any]) -> HttpResponse:
        # The confirmation page draws no form of its own to carry an error.
        refused(self.request, answer)
        return super()._refuse(form, answer)


def _deleted(result: Mapping[str, Any]) -> dict[str, Any]:
    """What a resend of a delete says: the label, since the record is gone."""

    return {"label": result["deleted"]["label"]}


def _show_on_form(form, error: ValidationError) -> None:
    """Put a model's refusal beside the field it names, or above the form."""

    for field, errors in error.update_error_dict({}).items():
        form.add_error(field if field in form.fields else None, errors)


class RecordFormMixin(PostedOnce, WriteMessages):
    """Create or update a plain record domain's record from its ``ModelForm``.

    For ``CreateView`` and ``UpdateView``; saved through
    ``application.records.save_form``, so noun, permission and target come
    from the domain's declaration. ``created_message`` and ``updated_message``
    may reword the announcement.
    """

    def form_valid(self, form: BaseForm) -> HttpResponse:
        adding = form.instance._state.adding
        try:
            ran = write_once(
                self.request,
                self._write,
                lambda: _saved(save_form(form, principal=web_principal(self.request.user)), adding=adding),
            )
        except ValidationError as error:
            _show_on_form(form, error)
            return self.form_invalid(form)
        return self._answered(form, ran.answer)

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        self.object = apps.get_model(answer["model"])._default_manager.get(pk=answer["pk"])
        template = self.created_message if answer["adding"] else self.updated_message
        noun = display_noun(records_for(type(self.object)))
        announce(self.request, template, noun, self.object)
        return redirect(self.object.get_absolute_url())


def _saved(instance: Any, *, adding: bool) -> dict[str, Any]:
    """What a resend of a record's form reloads it from, and whether it was made."""

    return {"model": instance._meta.label, "pk": instance.pk, "adding": adding}


class RecordDeleteMixin(ServiceDeleteMixin):
    """Delete a plain record domain's record through its declared deleter.

    For ``DeleteView``; ``deleted_message`` may reword the announcement. Takes
    only the confirmation page from ``ServiceDeleteMixin``: the service, noun
    and target come from the declaration.
    """

    @override
    def form_valid(self, form: BaseForm) -> HttpResponse:
        noun = display_noun(records_for(type(self.object)))
        ran = write_once(
            self.request,
            self._write,
            lambda: {
                **_deleted(delete_instance(self.object, principal=web_principal(self.request.user))),
                "noun": noun,
            },
        )
        return self._answered(form, ran.answer)

    @override
    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        announce(self.request, self.deleted_message, answer["noun"], answer["label"])
        return redirect(self.success_url)


class CommandFormMixin(PostedOnce):
    """A plain form whose submit is one service call: a *command* (adjust a
    threshold, open a period) posted from a page that then shows itself again.

    A domain's refusal becomes a form error beside its field rather than a 500.
    Subclasses declare ``service`` (as a ``staticmethod``), ``command`` (the
    command class, when the cleaned data maps straight onto one),
    ``success_url_name`` and ``success_message``.
    """

    service: Callable[..., Any] | None = None
    command: type[Any] | None = None
    success_url_name = ""
    success_message = ""
    # Which exceptions mean "the domain said no" rather than "HQ broke". Kept
    # narrow and overridable: catching more than this turns a real fault into a
    # form error and hides it.
    domain_errors: tuple[type[Exception], ...] = (ValueError, RecordNotFound, RecordConflict)

    def build_command(self, form: BaseForm) -> Any:
        return self.command(**form.cleaned_data) if self.command else form.cleaned_data

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    def get_success_message(self, result: Any) -> str:  # noqa: ARG002 - the default; an override is given the result
        return self.success_message

    def run(self, command: Any) -> Any:
        if self.service is None:
            raise ImproperlyConfigured(f"{type(self).__name__} declares no service.")
        return self.service(command, principal=web_principal(self.request.user))

    def form_valid(self, form: BaseForm) -> HttpResponse:
        try:
            ran = write_once(
                self.request, self._write, lambda: {"message": self._said(self.run(self.build_command(form)))}
            )
        except self.domain_errors as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        return self._answered(form, ran.answer)

    def _said(self, result: Any) -> str:
        """What the write says, as text a resend can say again."""

        return str(self.get_success_message(result) or "")

    def _replay(self, answer: dict[str, Any]) -> HttpResponse:
        if answer["message"]:
            messages.success(self.request, answer["message"])
        return redirect(self.get_success_url())


__all__ = [
    "CommandFormMixin",
    "RecordDeleteMixin",
    "RecordFormMixin",
    "ServiceCreateMixin",
    "ServiceDeleteMixin",
    "ServiceUpdateMixin",
    "ServiceWriteMixin",
    "WriteMessages",
]
