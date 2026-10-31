"""Declaring a resource: the generated create-and-edit form, adoption, and uploaded certificate material."""

from typing import get_origin

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from hq.platform.application.adoption import ADOPT, ADOPT_SERVICE
from hq.platform.application.asks import APPLIES_SHORTLY
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.certificates import UPLOAD
from hq.platform.application.entity_links import kind_label
from hq.platform.application.glance import DASHBOARD_MACHINE, dashboard_machine_selected
from hq.platform.application.infrastructure import resolved_spec, suggest_key
from hq.platform.application.naming import name_context
from hq.platform.application.pages import page_context
from hq.platform.application.plugins import import_reference
from hq.platform.application.provider_forms import (
    CertificateUploadForm,
    ResourceIdentityForm,
    spec_form_class,
)
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.ui import ended
from hq.platform.core import secrets

from .models import ManagedResource, OperationRequest
from .names import normalized_hostname
from .provider_adapters.declarations import MACHINE_KIND
from .provider_spec import NameContext
from .providers import PROVIDERS, controller_action_policy, describe_providers, readout_rows, spec_withheld


class ResourceFormView(View):
    """Declare or amend one resource, on a form its provider generates.

    The write is the ``infrastructure.resource.create`` or ``.update``
    capability the API and the MCP run, so its authority, validation and audit
    are theirs. This view supplies a form and a redirect and decides nothing else.
    """

    template_name = "control_plane/resource_form.html"

    def _existing(self, key):
        resource = get_object_or_404(ManagedResource, key=key) if key else None
        # A withheld kind's form would print its spec; its own page changes it.
        if resource is not None and spec_withheld(resource.kind):
            raise Http404
        return resource

    def _kind(self, request, resource):
        if resource:
            return resource.kind
        kind = request.GET.get("kind") or request.POST.get("kind") or ""
        if spec_withheld(kind):
            raise Http404
        return kind

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
                        provider for provider in describe_providers()["providers"] if not provider["created_from"]
                    ],
                    **page_context(
                        "What do you want to add?",
                        "HQ creates it and keeps it the way you set it.",
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
                "identity": (ResourceIdentityForm(initial={"enabled": resource.enabled}) if resource else None),
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
                    resource and kind == MACHINE_KIND and dashboard_machine_selected(resource.key)
                ),
                **_form_page(kind, resource),
            },
        )

    def post(self, request, key=None):
        resource = self._existing(key)
        kind = self._kind(request, resource)
        if kind not in PROVIDERS:
            raise Http404("Unknown type of record.")
        identity = ResourceIdentityForm(request.POST) if resource else None
        spec = spec_form_class(
            kind,
            lock_identity=bool(resource),
            context=_form_context(request, resource),
            # The same starting point the page was drawn from, so a value it
            # showed as held is one the form accepts back unchanged.
        )(request.POST, initial=_initial_spec(request, kind, resource))
        material_class = _material_form(kind) if not resource else None
        material = material_class(request.POST) if material_class else None
        if (identity is None or identity.is_valid()) and spec.is_valid() and (material is None or material.is_valid()):
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
                    resource and kind == MACHINE_KIND and dashboard_machine_selected(resource.key)
                ),
                **_form_page(kind, resource),
            },
        )

    def _declare(self, request, kind, resource, identity, spec, material):
        """Declare it, then act on the answer: a redirect, or None to re-render."""

        principal = web_principal(request.user)
        result = execute_capability(
            "infrastructure.resource.update" if resource else "infrastructure.resource.create",
            {
                # The identifier is never asked for again once a resource
                # exists, so an edit keeps the one it has.
                "key": resource.key if resource else _derived_key(kind, spec.spec),
                "kind": kind,
                "spec": spec.spec,
                # A thing being created is a thing you want applied.
                "enabled": identity.cleaned_data["enabled"] if identity else True,
            },
            principal=principal,
            target=resource.key if resource else None,
        )
        if not result["ok"]:
            spec.add_error(None, result["error"]["message"])
            return None
        saved = result["resource"]["key"]
        if resource and kind == MACHINE_KIND:
            shown = execute_capability(
                DASHBOARD_MACHINE, {"selected": "show_on_dashboard" in request.POST}, principal=principal, target=saved
            )
            if not shown["ok"]:
                messages.error(request, shown["error"]["message"])
        if material is not None:
            stored = execute_capability(
                PROVIDERS[kind].material_capability, dict(material.cleaned_data), principal=principal, target=saved
            )
            if not stored["ok"]:
                # The declaration exists and the material does not, so
                # say which half landed rather than reporting success.
                messages.error(request, stored["error"]["message"])
                return redirect("control_plane:upload_certificate", key=saved)
        messages.success(
            request,
            f"{'Added' if result['created'] else 'Updated'} “{saved}”. {APPLIES_SHORTLY}",
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

    Most resources are applied at the provider soon after. A kind
    whose actions are locked is not: a domain declaration records what HQ is
    responsible for and changes nothing. The capability registry's own reason
    is the honest answer, and it is written once, there.
    """

    applies, explanation = controller_action_policy(kind, OperationRequest.Action.RECONCILE)
    if applies:
        return APPLIES_SHORTLY
    return explanation


def _form_facts(resource, form) -> tuple[tuple[str, str, str], ...]:
    """What this resource is that the form below does not already ask.

    A certificate's edit page asks which target it installs on, not the names it
    covers or what is served with it, which is what a person comes to check. A row
    stays only when no form field has its label, and the identifier leads, which
    is why a machine's panel is not empty. Here rather than in the readout, which
    every list and detail surface shares and already identifies the resource by.
    """

    asked = {str(field.label).strip().casefold() for field in form}
    return (
        ("Identifier", "", resource.key),
        *(row for row in readout_rows(resource) if str(row[0]).strip().casefold() not in asked),
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

    A form is reached from wherever the thing appears (a service, a domain, a
    list), so the origin is carried explicitly rather than read from Referer, which
    is absent, stale or forged too often to navigate by, and checked, since an
    unvalidated redirect target from a query string is an open redirect.
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
    fields = PROVIDERS[resource.kind].spec_type.model_fields
    return {name: value for name, value in resolved.items() if name in fields and value not in (None, "", [], ())}


def _initial_spec(request, kind: str, resource) -> dict | None:
    """What the form says before anybody types in it.

    Editing starts from the declaration; creating, from query parameters the page
    the operator came from names spec fields with (a service page knows the
    hostname). Only the model's own fields are taken, so the URL cannot add a value
    the spec has no place for, and a provider joins this flow by having the field.
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
            except KeyError, TypeError, ValueError:
                hostname = ""
    return name_context(hostname)


def _material_form(kind: str):
    reference = PROVIDERS[kind].material_form
    return import_reference(reference) if reference else None


def _derived_key(kind: str, spec: dict) -> str:
    """A name for a declaration the operator did not want to name.

    The provider says what its own records should be called, and the same
    function answers here, at adoption, and during onboarding.
    """

    return suggest_key(kind, spec)


class AdoptView(View):
    """Bring something the provider already holds under HQ's management.

    One click, no form. The spec is read back out of the live record, so the
    declaration starts equal to the world and the first reconciliation changes
    nothing, which is the only reason adopting is safe to do without asking
    the operator to confirm every field first.
    """

    def post(self, request, hostname):
        result = execute_capability(ADOPT_SERVICE, {"hostname": hostname}, principal=web_principal(request.user))
        if not result["ok"]:
            messages.error(request, result["error"]["message"])
            return redirect("control_plane:services")
        adopted = ", ".join(result["adopted"])
        messages.success(
            request,
            f"HQ now manages {hostname} as {ended(adopted)} Nothing live was changed.",
        )
        return redirect("control_plane:service", hostname=result["hostname"])


class CertificateUploadView(View):
    """Take a certificate generated elsewhere and hold it for installation."""

    template_name = "control_plane/certificate_upload.html"

    @staticmethod
    def _page(resource) -> dict:
        return page_context(
            f"Certificate for {resource.key}",
            "A certificate and key issued by your offline CA. HQ installs it "
            "wherever this certificate is used and keeps it for reuse.",
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
            stored = execute_capability(
                UPLOAD, dict(form.cleaned_data), principal=web_principal(request.user), target=resource.key
            )
            if not stored["ok"]:
                form.add_error(None, stored["error"]["message"])
            else:
                messages.success(
                    request,
                    f"Stored a certificate for {ended(', '.join(stored['domains']))} {APPLIES_SHORTLY}",
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


class AdoptRecordView(View):
    """Take on one record a sweep found, identified by what makes it that record.

    Separate from adopting a service, which is a hostname, because some records
    have none: a container answers wherever its ports are pointed. The spec is read
    back out of the sweep, not posted, so the first reconciliation changes nothing.
    """

    def post(self, request, kind, token):
        result = execute_capability(ADOPT, {"kind": kind, "token": token}, principal=web_principal(request.user))
        if not result["ok"]:
            messages.error(request, result["error"]["message"])
        else:
            messages.success(
                request,
                f"HQ now manages “{result['resource']['key']}”. Nothing live was changed.",
            )
        return redirect(safe_next(request, fallback=reverse("control_plane:list")))
