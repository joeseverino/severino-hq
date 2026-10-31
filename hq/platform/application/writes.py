"""Form views whose write is one application service call.

The write side of what ``TableListMixin`` is for the read side: create, update
and delete share the same steps (command, service call, reload, announce,
redirect), so they are declared once and a plugin adding a domain inherits
them. A view declares only which service, which record and what it is called;
a plain ``Records`` domain's view names only its form.
"""

from collections.abc import Mapping
from typing import Any, override

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.shortcuts import redirect
lazy from django.forms import BaseForm
lazy from django.http import HttpResponse

from hq.platform.application.routes import reverse

from .deletion import DeleteCommand
from .record_errors import RecordConflict, RecordNotFound
from .records import delete_instance, display_noun, records_for, save_form
from .security import web_principal
from .ui import ended
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


class ServiceWriteMixin(WriteMessages):
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


class ServiceCreateMixin(ServiceWriteMixin):
    """Create through a service, then go to what was created."""

    def form_valid(self, form: BaseForm) -> HttpResponse:
        result = self.service(
            self.command_from_cleaned_data(form.cleaned_data),
            principal=self.write_principal(),
        )
        self.object = self.reload(result)
        self.announce(self.created_message, self.object)
        return redirect(self.object.get_absolute_url())


class ServiceUpdateMixin(ServiceWriteMixin):
    """Amend through a service, then go to where it ended up.

    Reloaded by the identity the service returned, not the one that arrived:
    a rename changes the slug, and redirecting to the old one 404s.
    """

    def form_valid(self, form: BaseForm) -> HttpResponse:
        result = self.service(
            self.command_from_cleaned_data(form.cleaned_data),
            principal=self.write_principal(),
            **{self.identity_kwarg: self.current_identity()},
        )
        self.object = self.reload(result)
        self.announce(self.updated_message, self.object)
        return redirect(self.object.get_absolute_url())


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

    def form_valid(self, form: BaseForm) -> HttpResponse:  # noqa: ARG002 - Django's FormMixin calls it with the form
        identity = self.current_identity()
        result = self.service(
            DeleteCommand(confirm=str(identity)),
            principal=self.write_principal(),
            **{self.identity_kwarg: identity},
        )
        self.announce(self.deleted_message, result["deleted"]["label"])
        return redirect(self.success_url)


def _show_on_form(form, error: ValidationError) -> None:
    """Put a model's refusal beside the field it names, or above the form."""

    for field, errors in error.update_error_dict({}).items():
        form.add_error(field if field in form.fields else None, errors)


class RecordFormMixin(WriteMessages):
    """Create or update a plain record domain's record from its ``ModelForm``.

    For ``CreateView`` and ``UpdateView``; saved through
    ``application.records.save_form``, so noun, permission and target come
    from the domain's declaration. ``created_message`` and ``updated_message``
    may reword the announcement.
    """

    def form_valid(self, form: BaseForm) -> HttpResponse:
        adding = form.instance._state.adding
        try:
            self.object = save_form(form, principal=web_principal(self.request.user))
        except ValidationError as error:
            _show_on_form(form, error)
            return self.form_invalid(form)
        template = self.created_message if adding else self.updated_message
        noun = display_noun(records_for(type(self.object)))
        announce(self.request, template, noun, self.object)
        return redirect(self.object.get_absolute_url())


class RecordDeleteMixin(ServiceDeleteMixin):
    """Delete a plain record domain's record through its declared deleter.

    For ``DeleteView``; ``deleted_message`` may reword the announcement. Takes
    only the confirmation page from ``ServiceDeleteMixin``: the service, noun
    and target come from the declaration.
    """

    @override
    def form_valid(self, form: BaseForm) -> HttpResponse:
        noun = display_noun(records_for(type(self.object)))
        result = delete_instance(self.object, principal=web_principal(self.request.user))
        template = self.deleted_message
        announce(self.request, template, noun, result["deleted"]["label"])
        return redirect(self.success_url)


class CommandFormMixin:
    """A plain form whose submit is one service call: a *command* (adjust a
    threshold, open a period) posted from a page that then shows itself again.

    A domain's refusal becomes a form error beside its field rather than a 500.
    Subclasses declare ``service`` (as a ``staticmethod``), ``command`` (the
    command class, when the cleaned data maps straight onto one),
    ``success_url_name`` and ``success_message``.
    """

    service = None
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
        return self.service(command, principal=web_principal(self.request.user))

    def form_valid(self, form: BaseForm) -> HttpResponse:
        try:
            result = self.run(self.build_command(form))
        except self.domain_errors as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        message = self.get_success_message(result)
        if message:
            messages.success(self.request, message)
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
