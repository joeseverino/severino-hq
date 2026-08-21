"""Form views whose write is one application service call.

The read side of a list page is already declarative -- a view names its filters
and sorts and ``TableListMixin`` derives the rest. The write side was not: every
create, update and delete in every domain restated the same five steps by hand,
so the same five steps could drift five ways, and a plugin adding a sixth domain
had nothing to inherit.

The steps never varied. Build a command from the cleaned data, call the service
with a web principal, reload the record the service names, say what happened,
and go to it. What varies is only which service, which record, and what the
thing is called -- so those are what a view declares here.

Reloading is deliberate rather than wasteful. Services return serialisable
results because the API and the MCP share them; a Django view needs a model
instance for ``get_absolute_url`` and ``__str__``. The alternative is teaching
the view to rebuild the URL itself, which duplicates knowledge the model
already owns. One query at the boundary is the cheaper trade.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import redirect
from django.urls import reverse

from .deletion import DeleteCommand
from .security import web_principal


class ServiceWriteMixin:
    """Shared spine for a view whose mutation is one service call.

    Subclasses declare:

    ``service``
        The application service, wrapped in ``staticmethod`` so attribute
        access does not bind it as a method.
    ``result_key``
        The key the service nests its record under, e.g. ``"asset"``.
    ``identity_attr``
        The model attribute carrying identity, e.g. ``"slug"`` or ``"pk"``.
    ``identity_result_key``
        That same identity's key inside the result, when it is spelled
        differently there -- ``pk`` on the model is ``id`` in the payload.
        Defaults to ``identity_attr``.
    ``identity_kwarg``
        The keyword naming the record being amended, e.g. ``"current_slug"``.
        Not used when creating.
    ``noun``
        What to call the thing in a message, e.g. ``"Content item"``.

    Declared here as a contract and not as attributes. A placeholder value that
    is never correct -- an empty noun, an empty result key -- collides with the
    real one on whichever base supplies it, so which wins depends on the order
    the bases were written in and reordering them breaks the view silently. It
    also turns "forgot to set this" into a message reading "“Ada” created" with
    the noun missing, rather than into an error.

    ``identity_attr`` has a default because a slug genuinely is the common case
    and every value it takes is a working one.
    """

    identity_attr = "slug"

    #: Read through ``getattr`` so a subclass that omits one is an error naming
    #: the attribute, rather than a blank in a sentence.
    REQUIRED = ("service", "noun", "result_key")

    def __init_subclass__(cls, **kwargs):
        # A plain function assigned to a class attribute becomes a bound method
        # on access, so ``self.service(command, ...)`` would silently pass the
        # view as the command. Caught at class creation, where the fix is
        # obvious, rather than at the first write, where it is not.
        super().__init_subclass__(**kwargs)
        for name in ("service", "command_from_cleaned_data"):
            value = cls.__dict__.get(name)
            if callable(value) and not isinstance(value, staticmethod):
                raise ImproperlyConfigured(
                    f"{cls.__name__}.{name} must be wrapped in staticmethod()."
                )
        # Only a class that supplies a service is a view rather than another
        # layer of mixin, so only that class has to be complete.
        if "service" not in cls.__dict__:
            return
        missing = [
            name for name in cls.REQUIRED if getattr(cls, name, None) in (None, "")
        ]
        if missing:
            raise ImproperlyConfigured(
                f"{cls.__name__} writes through a service and declares no "
                f"{', '.join(missing)}."
            )

    def write_principal(self):
        return web_principal(self.request.user)

    def current_identity(self):
        return getattr(self.get_object(), self.identity_attr)

    def reload(self, result):
        """The saved record, as a model instance the URL can be taken from."""

        key = getattr(self, "identity_result_key", "") or self.identity_attr
        return self.model._default_manager.get(
            **{self.identity_attr: result[self.result_key][key]}
        )

    def announce(self, template: str, target) -> None:
        messages.success(self.request, template.format(noun=self.noun, target=target))


class ServiceCreateMixin(ServiceWriteMixin):
    """Create through a service, then go to what was created."""

    created_message = "{noun} “{target}” created."

    def form_valid(self, form):
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

    updated_message = "{noun} “{target}” updated."

    def form_valid(self, form):
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

    deleted_message = "{noun} “{target}” deleted."

    def form_valid(self, form):
        identity = self.current_identity()
        result = self.service(
            DeleteCommand(confirm=str(identity)),
            principal=self.write_principal(),
            **{self.identity_kwarg: identity},
        )
        self.announce(self.deleted_message, result["deleted"]["label"])
        return redirect(self.success_url)


class CommandFormMixin:
    """A plain form whose submit is one service call.

    The three mixins above cover a record with a URL of its own: create it,
    amend it, delete it, go to it. Plugins mostly do not have that shape. They
    have a *command* -- adjust a threshold, open a period, correct a recorded
    value -- posted from a page that then shows itself again.

    That shape repeated too, and identically: build a command, call the service
    with a web principal, turn the domain's refusal into a form error rather
    than a traceback, say what happened, redirect somewhere fixed. The fourth
    step is the one worth sharing. A service that raises ``ValueError`` for
    "you cannot do that yet" is reporting a domain rule, and a rule belongs on
    the form beside the field it concerns -- not on a 500 page.

    Subclasses declare ``service`` (as a ``staticmethod``), ``command`` (the
    command class, if the cleaned data maps straight onto one), ``success_url_name``
    and ``success_message``.
    """

    service = None
    command = None
    success_url_name = ""
    success_message = ""
    # Which exceptions mean "the domain said no" rather than "HQ broke". Kept
    # narrow and overridable: catching more than this turns a real fault into a
    # form error and hides it.
    domain_errors: tuple[type[Exception], ...] = (ValueError,)

    def build_command(self, form):
        return self.command(**form.cleaned_data) if self.command else form.cleaned_data

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    def get_success_message(self, result) -> str:
        return self.success_message

    def run(self, command):
        return self.service(command, principal=web_principal(self.request.user))

    def form_valid(self, form):
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
    "ServiceCreateMixin",
    "ServiceDeleteMixin",
    "ServiceUpdateMixin",
    "ServiceWriteMixin",
]
