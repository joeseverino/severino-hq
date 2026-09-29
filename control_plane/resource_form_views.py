"""Declaring a resource: the generated create-and-edit form, adoption, and uploaded certificate material."""

from __future__ import annotations

from typing import get_origin

from django.contrib import messages

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from application.infrastructure import (
    ManagedResourceCommand,
    NotFoundError,
    PolicyError,
    resolved_spec,
    save_managed_resource,
    suggest_key,
)
from application.glance import dashboard_machine_selected, select_dashboard_machine
from application.adoption import (
    AdoptCommand,
    AdoptServiceCommand,
    adopt,
    adopt_service,
)
from application.certificates import (
    CertificateError,
    UploadCertificateCommand,
    store_certificate,
)
from application.entity_links import kind_label
from application.resource_context import readout_rows
from application.naming import name_context
from application.plugins import _import
from application.provider_forms import (
    CertificateUploadForm,
    ResourceIdentityForm,
    spec_form_class,
)
from application.security import safe_next, web_principal
from application.pages import page_context

from core import secrets

from .models import ManagedResource, OperationRequest
from .names import normalized_hostname
from .provider_adapters.declarations import MACHINE_KIND
from .provider_spec import NameContext
from .providers import PROVIDERS, controller_action_policy, describe_providers


class ResourceFormView(LoginRequiredMixin, View):
    """Declare or amend one resource, on a form its provider generates.

    The write goes through ``save_managed_resource`` (the same use case the
    API and the MCP call) so the capability check, the spec validation, the
    generation bump and the audit record are the ones that already existed. This
    view supplies a form and a redirect and decides nothing else.
    """

    template_name = "control_plane/resource_form.html"

    def _existing(self, key):
        return get_object_or_404(ManagedResource, key=key) if key else None

    def _kind(self, request, resource):
        if resource:
            return resource.kind
        return request.GET.get("kind") or request.POST.get("kind") or ""

    def get(self, request, key=None):
        resource = self._existing(key)
        kind = self._kind(request, resource)
        if kind not in PROVIDERS:
            return render(
                request,
                "control_plane/resource_kind.html",
                {
                    # Only the kinds that stand on their own. One that declares
                    # a surface of its own is created from there, where the
                    # context it needs is already established.
                    "providers": [
                        provider
                        for provider in describe_providers()["providers"]
                        if not provider["created_from"]
                    ],
                    **page_context(
                        "What do you want to add?",
                        "HQ creates it at the provider and keeps it in sync.",
                    ),
                },
            )
        material_class = _material_form(kind) if not resource else None
        material = material_class() if material_class else None
        # Built before the context, because the facts panel above it is decided
        # by which questions it turns out to be asking.
        spec = spec_form_class(
            kind,
            lock_identity=bool(resource),
            context=_form_context(request, resource),
        )(initial=_initial_spec(request, kind, resource))
        return render(
            request,
            self.template_name,
            {
                "kind": kind,
                "resource": resource,
                "label": kind_label(kind),
                "summary": PROVIDERS[kind].summary,
                # Only when editing. Creating something asks what HQ cannot
                # know and nothing else: a name it can derive, and a pause
                # switch for a thing that does not exist yet, are not questions.
                "identity": (
                    ResourceIdentityForm(initial={"enabled": resource.enabled})
                    if resource
                    else None
                ),
                # The form is built knowing which name it is about, so its
                # menus can offer what suits that name rather than everything
                # that exists. A certificate menu with one entry is right by
                # luck; a second certificate is what makes it a question.
                "spec": spec,
                # Collected here rather than on a page of its own. A resource
                # that is not usable without material should not be creatable
                # without it.
                "material": material,
                "cancel_url": _cancel_url(request, kind, resource),
                "apply_note": _apply_note(kind),
                # What this resource already is, when editing one. A form whose
                # fields are mostly derived elsewhere shows empty boxes and
                # nothing else, and an edit page for a certificate would say
                # nothing about the names it covers or where it is installed,
                # which is the whole of what a person comes to check.
                "facts": _form_facts(resource, spec) if resource else (),
                "show_on_dashboard": bool(
                    resource
                    and kind == MACHINE_KIND
                    and dashboard_machine_selected(resource.key)
                ),
                **_form_page(kind, resource),
            },
        )

    def post(self, request, key=None):
        resource = self._existing(key)
        kind = self._kind(request, resource)
        if kind not in PROVIDERS:
            raise Http404("Unknown provider kind.")
        identity = ResourceIdentityForm(request.POST) if resource else None
        spec = spec_form_class(
            kind,
            lock_identity=bool(resource),
            context=_form_context(request, resource),
        )(request.POST, initial=resource.spec if resource else None)
        material_class = _material_form(kind) if not resource else None
        material = material_class(request.POST) if material_class else None
        if (
            (identity is None or identity.is_valid())
            and spec.is_valid()
            and (material is None or material.is_valid())
        ):
            response = self._declare(request, kind, resource, identity, spec, material)
            if response is not None:
                return response
        return render(
            request,
            self.template_name,
            {
                "kind": kind,
                "resource": resource,
                "label": kind_label(kind),
                "summary": PROVIDERS[kind].summary,
                "identity": identity,
                "spec": spec,
                "material": material,
                "show_on_dashboard": bool(
                    resource
                    and kind == MACHINE_KIND
                    and dashboard_machine_selected(resource.key)
                ),
                **_form_page(kind, resource),
            },
        )

    def _declare(self, request, kind, resource, identity, spec, material):
        """Declare it, then act on the answer: a redirect, or None to re-render."""

        try:
            result = save_managed_resource(
                ManagedResourceCommand(
                    # The identifier is never asked for again once a
                    # resource exists, so an edit keeps the one it has.
                    key=resource.key if resource else _derived_key(kind, spec.spec),
                    kind=kind,
                    spec=spec.spec,
                    # A thing being created is a thing you want applied.
                    enabled=identity.cleaned_data["enabled"] if identity else True,
                ),
                principal=web_principal(request.user),
                current_key=resource.key if resource else None,
            )
        except (PolicyError, DjangoValidationError) as exc:
            spec.add_error(None, _readable_error(exc))
            return None
        saved = result["resource"]["key"]
        if resource and kind == MACHINE_KIND:
            select_dashboard_machine(
                saved,
                selected="show_on_dashboard" in request.POST,
                principal=web_principal(request.user),
            )
        if material is not None:
            try:
                _store_material(kind, saved, material.cleaned_data, request)
            except (CertificateError, secrets.SecretsUnavailable) as exc:
                # The declaration exists and the material does not, so
                # say which half landed rather than reporting success.
                messages.error(request, str(exc))
                return redirect("control_plane:upload_certificate", key=saved)
        messages.success(
            request,
            f"{'Added' if result['created'] else 'Updated'} “{saved}”. "
            "Applies at the provider within about a minute.",
        )
        # Back where the operator was working. Publishing a service
        # takes two or three declarations, and landing on each one's
        # own page after saving would make the next step a navigation
        # problem: the service page is the thing being built.
        return redirect(_after_save(request, kind, resource, saved))


def _form_page(kind: str, resource) -> dict:
    """The head of the resource form, the same on a first render and a retry."""

    label = kind_label(kind)
    return page_context(
        f"Edit {resource.key}" if resource else f"Add {label}",
        PROVIDERS[kind].summary,
    )


def _apply_note(kind: str) -> str:
    """What actually happens after saving, which is not the same for every kind.

    Most resources are applied at the provider within about a minute. A kind
    whose actions are locked is not: a domain declaration records what HQ is
    responsible for and changes nothing. The capability registry's own reason
    is the honest answer, and it is written once, there.
    """

    applies, explanation = controller_action_policy(
        kind, OperationRequest.Action.RECONCILE
    )
    if applies:
        return "Applies at the provider within about a minute."
    return explanation


def _form_facts(resource, form) -> tuple[tuple[str, str, str], ...]:
    """What this resource is that the form below does not already ask.

    The panel exists for the fields a form cannot show: a certificate's edit
    page asks which target it installs on and says nothing about the names it
    covers or what is actually served with it, which is the whole of what a
    person comes to check.

    A machine's form already holds every field, so repeating them here would
    say each one twice. Matched by label against the form's own fields, a row
    survives only when nothing below is asking about it.

    The identifier leads, and is the reason this is not empty for a machine.

    Added here rather than in the readout, which every list and detail surface
    shares: they identify the resource in their own way already, and a row
    repeating it on each of them is the same fact three times.
    """

    asked = {str(field.label).strip().casefold() for field in form}
    return (("Identifier", "", resource.key),) + tuple(
        row
        for row in readout_rows(resource)
        if str(row[0]).strip().casefold() not in asked
    )


def _after_save(request, kind: str, resource, saved: str) -> str:
    """Where saving lands: back at whatever this was being added to.

    Same rule as Cancel, and for the same reason: the surface an operator came
    from is the one that knows what is still missing.
    """

    returning = _return_to(request)
    if returning:
        return returning
    if resource is None:
        origin = _cancel_url(request, kind, None)
        if origin != reverse("control_plane:list"):
            return origin
    return reverse("control_plane:detail", kwargs={"key": saved})


def _return_to(request) -> str:
    """The page that sent the operator here, if it said so and is ours.

    A form is reached from wherever the thing being edited appears (a
    service, a domain, a list), and returning to the resource instead puts the
    operator somewhere they have not been, several clicks from the page they
    are working on. The origin is carried explicitly rather than guessed from
    Referer, which is absent, stale or forged often enough not to navigate by.

    Checked before use: an unvalidated redirect target taken from a query
    string is an open redirect regardless of how friendly the link looked.
    """

    return safe_next(request)


def _cancel_url(request, kind: str, resource) -> str:
    """Where "Cancel" belongs: back where the operator came from.

    The page that linked here wins, because it is the only one that actually
    knows. Failing that, editing returns to the resource and creating returns
    to the surface the provider says offered it, so a record added from a
    domain goes back to that domain rather than to the resource registry.
    """

    returning = _return_to(request)
    if returning:
        return returning
    if resource:
        return reverse("control_plane:detail", kwargs={"key": resource.key})
    if PROVIDERS[kind].created_from == "zone":
        zone = request.GET.get("zone", "").strip()
        if zone:
            return reverse("zones:detail", kwargs={"zone": zone})
        return reverse("zones:index")
    hostname = normalized_hostname(request.GET.get("hostname", ""))
    if hostname:
        return reverse("control_plane:service", kwargs={"hostname": hostname})
    return reverse("control_plane:list")


def _derived_spec(resource) -> dict:
    """Spec values another source still supplies, so a form can show them.

    Only fields the declaration has left empty: anything authored already wins,
    and nothing here can quietly replace an operator's answer.
    """

    try:
        resolved = resolved_spec(resource)
    except Exception:  # noqa: BLE001 - a form must render without the topology
        return {}
    if not isinstance(resolved, dict):
        return {}
    fields = PROVIDERS[resource.kind].spec_type.model_fields
    return {
        name: value
        for name, value in resolved.items()
        if name in fields and value not in (None, "", [], ())
    }


def _initial_spec(request, kind: str, resource) -> dict | None:
    """What the form says before anybody types in it.

    Editing starts from the declaration. Creating starts from wherever the
    operator came from, and that context arrives as query parameters naming spec
    fields: a service page knows the hostname, a zone page knows the domain.
    Filtered against the model's own fields, so the URL cannot introduce a value
    the spec has no place for, and a provider joins this flow by having the
    field rather than by this view being taught about it.
    """

    if resource:
        # A field the declaration leaves blank because something else still
        # answers it is shown holding that answer, so the form states what is
        # true rather than an empty box beside a page saying otherwise. Saving
        # writes it in, which is how a derived value becomes an authored one,
        # the same adoption every record goes through, and equally a no-op the
        # first time.
        return {**_derived_spec(resource), **resource.spec}
    provider = PROVIDERS[kind]
    initial: dict = {}
    hostname = request.GET.get("hostname", "").strip()
    if provider.seed and hostname:
        initial.update(provider.seed(name_context(hostname)))
    for name, field in provider.spec_type.model_fields.items():
        if get_origin(field.annotation) is list:
            values = [item.strip() for item in request.GET.getlist(name) if item.strip()]
            if values:
                initial[name] = values
            continue
        value = request.GET.get(name, "").strip()
        if value:
            initial[name] = value
    return initial or None


def _form_context(request, resource) -> NameContext:
    """What HQ knows about the name this form is about.

    On create the name arrives in the query string, the same way every other
    seeded value does. On edit it is read back out of the resource through its
    own provider, because a form has to keep offering whatever the record
    already holds: a menu that cannot describe an existing value tells the
    operator their unmodified record is invalid.
    """

    hostname = request.GET.get("hostname", "").strip()
    if not hostname and resource is not None:
        provider = PROVIDERS.get(resource.kind)
        if provider is not None and provider.hostnames is not None:
            try:
                hostname = next(iter(provider.hostnames(resource.spec)), "")
            except (KeyError, TypeError, ValueError):
                hostname = ""
    return name_context(hostname)


def _material_form(kind: str):
    reference = PROVIDERS[kind].material_form
    return _import(reference) if reference else None


def _store_material(kind: str, key: str, cleaned: dict, request) -> None:
    _import(PROVIDERS[kind].material_handler)(
        key, cleaned, principal=web_principal(request.user)
    )


def _derived_key(kind: str, spec: dict) -> str:
    """A name for a declaration the operator did not want to name.

    The provider says what its own records should be called, and the same
    function answers here, at adoption, and during onboarding.
    """

    return suggest_key(kind, spec)


def _readable_error(exc) -> str:
    """What to show an operator when a use case refuses.

    Django collects several messages on one ValidationError, and str() of that
    renders the list with its brackets and quotes intact. Shared with the domain
    views, so one refusal reads the same on every page.
    """

    messages_found = getattr(exc, "messages", None)
    return " ".join(messages_found) if messages_found else str(exc)


class AdoptView(LoginRequiredMixin, View):
    """Bring something the provider already holds under HQ's management.

    One click, no form. The spec is read back out of the live record, so the
    declaration starts equal to the world and the first reconciliation changes
    nothing, which is the only reason adopting is safe to do without asking
    the operator to confirm every field first.
    """

    def post(self, request, hostname):
        try:
            result = adopt_service(
                AdoptServiceCommand(hostname=hostname),
                principal=web_principal(request.user),
            )
        except (NotFoundError, PolicyError, DjangoValidationError) as exc:
            messages.error(request, _readable_error(exc))
            return redirect("control_plane:services")
        adopted = ", ".join(result["adopted"])
        messages.success(
            request,
            f"Adopted {hostname} as {adopted}. Nothing changed at the provider.",
        )
        return redirect("control_plane:service", hostname=result["hostname"])


class CertificateUploadView(LoginRequiredMixin, View):
    """Take a certificate generated elsewhere and hold it for installation."""

    template_name = "control_plane/certificate_upload.html"

    @staticmethod
    def _page(resource) -> dict:
        return page_context(
            f"Certificate for {resource.key}",
            "A certificate and key issued by your offline CA. HQ installs it on "
            "this resource's consumers and keeps it for reuse.",
        )

    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        return render(
            request,
            self.template_name,
            {
                "resource": resource,
                "form": CertificateUploadForm(),
                "store_ready": secrets.available(),
                "material": getattr(resource, "material", None),
                **self._page(resource),
            },
        )

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        form = CertificateUploadForm(request.POST)
        if form.is_valid():
            try:
                stored = store_certificate(
                    UploadCertificateCommand(
                        key=resource.key,
                        fullchain=form.cleaned_data["fullchain"],
                        private_key=form.cleaned_data["private_key"],
                    ),
                    principal=web_principal(request.user),
                )
            except (CertificateError, secrets.SecretsUnavailable, PolicyError) as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"Stored a certificate for {', '.join(stored['domains'])}. "
                    "It installs on the next controller pass.",
                )
                return redirect("control_plane:detail", key=resource.key)
        return render(
            request,
            self.template_name,
            {
                "resource": resource,
                "form": form,
                "store_ready": secrets.available(),
                "material": getattr(resource, "material", None),
                **self._page(resource),
            },
        )


class AdoptRecordView(LoginRequiredMixin, View):
    """Take on one record a sweep found, identified by what makes it that record.

    Separate from adopting a service because a service is a hostname and some
    records have none: a container answers wherever its ports are pointed, so
    there is no name to route by and no group of records to take on together.

    The spec is read back out of the sweep rather than posted, the same as every
    other adoption: the declaration starts equal to the world, so the first
    reconciliation after it changes nothing.
    """

    def post(self, request, kind, token):
        try:
            result = adopt(
                AdoptCommand(kind=kind, token=token),
                principal=web_principal(request.user),
            )
        except (NotFoundError, PolicyError, ValueError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Adopted “{result['resource']['key']}”. Nothing changed.",
            )
        return redirect(safe_next(request, fallback=reverse("control_plane:list")))
