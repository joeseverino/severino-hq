from __future__ import annotations

import uuid
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from application.infrastructure import (
    ManagedResourceCommand,
    NotFoundError,
    OperationCommand,
    PolicyError,
    certificate_renewal_allowed,
    controller_contract,
    operation_summary,
    request_certificate_renewal,
    request_lifecycle,
    request_reconcile,
    request_removal,
    resource_health,
    resolved_spec,
    save_managed_resource,
    serialize_resource,
    serialize_public_status,
    suggest_key,
)
from application.inventory import (
    AdoptCommand,
    AdoptServiceCommand,
    adopt,
    adopt_service,
    inventory_state,
    unmanaged_services,
)
from application.certificates import (
    CertificateError,
    UploadCertificateCommand,
    store_certificate,
)
from application.connections import connection_readings
from application.machines import container_context, machine, machine_catalog
from application.services import machine_link
from application.naming import name_context
from application.plugins import _import
from application.provider_forms import (
    CertificateUploadForm,
    ResourceIdentityForm,
    spec_form_class,
)
from application.security import safe_next, web_principal
from application.service_context import sections_for
from application.services import (
    CONTAINER_KIND,
    alias_target,
    service_catalog,
    service_or_prospect,
)

from core import secrets

from .models import ManagedResource, OperationRequest
from .providers import (
    CERTIFICATE_KIND,
    DELIVERY_TARGET_KIND,
    NameContext,
    PROVIDERS,
    normalized_hostname,
    service_facets,
    controller_action_policy,
    describe_providers,
)


# Which use case serves which verb. A ladder here meant every verb but one fell
# through to reconciliation: pressing Restart queued a reconcile, which is
# locked for a container, so the button reported a policy error and did nothing.
_OPERATION_USE_CASE = {
    OperationRequest.Action.RECONCILE: request_reconcile,
    OperationRequest.Action.RENEW: request_certificate_renewal,
}


def _web_operation(request, resource, action):
    command = OperationCommand(
        idempotency_key=f"web:{request.user.pk}:{uuid.uuid4()}",
        reason=request.POST.get("reason", "").strip(),
    )
    principal = web_principal(request.user)
    use_case = _OPERATION_USE_CASE.get(action)
    if use_case is not None:
        return use_case(command, principal=principal, current_key=resource.key)
    # Everything else is a lifecycle verb: asked for once, about something
    # already as declared. One entry point rather than one function per verb,
    # because they differ only in the word.
    return request_lifecycle(
        command, principal=principal, current_key=resource.key, action=action
    )


class ResourceFormView(LoginRequiredMixin, View):
    """Declare or amend one resource, on a form its provider generates.

    The write goes through ``save_managed_resource`` -- the same use case the
    API and the MCP call -- so the capability check, the spec validation, the
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
                    ]
                },
            )
        material_class = _material_form(kind) if not resource else None
        material = material_class() if material_class else None
        return render(
            request,
            self.template_name,
            {
                "kind": kind,
                "resource": resource,
                "label": PROVIDERS[kind].label or kind,
                "summary": PROVIDERS[kind].summary,
                # Only when editing. Creating something asks what HQ cannot
                # know and nothing else: a name it can derive, and a pause
                # switch for a thing that does not exist yet, are not questions.
                "identity": (
                    ResourceIdentityForm(
                        initial={"key": resource.key, "enabled": resource.enabled}
                    )
                    if resource
                    else None
                ),
                # The form is built knowing which name it is about, so its
                # menus can offer what suits that name rather than everything
                # that exists. A certificate menu with one entry was right by
                # luck; the second certificate is what makes it a question.
                "spec": spec_form_class(
                    kind,
                    lock_identity=bool(resource),
                    context=_form_context(request, resource),
                )(initial=_initial_spec(request, kind, resource)),
                # Collected here rather than on a page of its own. A resource
                # that is not usable without material should not be creatable
                # without it.
                "material": material,
                "cancel_url": _cancel_url(request, kind, resource),
                "apply_note": _apply_note(kind),
                # What this resource already is, when editing one. A form whose
                # fields are mostly derived elsewhere shows empty boxes and
                # nothing else -- an edit page for a certificate said nothing
                # about the names it covers or where it is installed, which is
                # the whole of what a person came to check.
                "facts": _readout_rows(resource) if resource else (),
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
            try:
                result = save_managed_resource(
                    ManagedResourceCommand(
                        key=(
                            identity.cleaned_data["key"] or resource.key
                            if identity
                            else _derived_key(kind, spec.spec)
                        ),
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
            else:
                saved = result["resource"]["key"]
                if material is not None:
                    try:
                        _store_material(kind, saved, material.cleaned_data, request)
                    except (CertificateError, secrets.SecretsUnavailable) as exc:
                        # The declaration exists and the material does not, so
                        # say which half landed rather than reporting success.
                        messages.error(request, str(exc))
                        return redirect(
                            "control_plane:upload_certificate", key=saved
                        )
                messages.success(
                    request,
                    f"{'Added' if result['created'] else 'Updated'} “{saved}”. "
                    "HQ will apply it at the provider within about a minute.",
                )
                # Back where the operator was working. Publishing a service
                # takes two or three declarations, and landing on each one's
                # own page after saving it made the next step a navigation
                # problem -- the service page is the thing being built.
                return redirect(_after_save(request, kind, resource, saved))
        return render(
            request,
            self.template_name,
            {
                "kind": kind,
                "resource": resource,
                "label": PROVIDERS[kind].label or kind,
                "summary": PROVIDERS[kind].summary,
                "identity": identity,
                "spec": spec,
                "material": material,
            },
        )


def _spec_value(value: Any) -> str:
    """One spec field as a person reads it.

    A list rendered straight into a template comes out as its Python repr, so
    the last thing shown before a destructive action was
    ``['private.jseverino.com']`` -- brackets, quotes and all.
    """

    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _spec_rows(resource) -> dict[str, tuple[tuple[str, str], ...]]:
    """A spec as an operator reads it, split the way the form splits it.

    Three things this fixes, all of them seen on the confirmation page shown
    before a destructive action:

    - Raw field names. It listed "record_type" and "ttl" -- the same failure
      that once asked an operator for a "Topology ref". The titles already
      exist on the model.
    - Unset optionals. A field nobody filled in rendered as "None", which is
      Python's word for it and nobody else's.
    - Every tuning knob at equal weight. Fifteen rows, of which "Hsts
      subdomains: False" and "Access list: 0" are defaults nobody chose, buried
      the four that say what this actually is. The provider already declares
      which of its fields are routine; this is the same split the form makes.
    """

    provider = PROVIDERS[resource.kind]
    fields = provider.spec_type.model_fields
    primary: list[tuple[str, str]] = []
    advanced: list[tuple[str, str]] = []
    for name, value in resource.spec.items():
        if value is None:
            continue
        label = (
            fields[name].title or name.replace("_", " ").capitalize()
            if name in fields
            else name
        )
        row = (label, _spec_value(value))
        (advanced if name in provider.advanced_fields else primary).append(row)
    return {"primary": tuple(primary), "advanced": tuple(advanced)}


def _origin_machine(resource):
    """The machine a resource forwards to, if its provider says where it serves."""

    provider = PROVIDERS.get(resource.kind)
    if provider is None or provider.origin is None:
        return None
    try:
        origin = provider.origin(resolved_spec(resource))
    except (KeyError, TypeError, ValueError):
        return None
    return machine_link(origin) if origin else None


def _service_links(resource) -> tuple[tuple[str, str], ...]:
    """``(hostname, url)`` for every service this resource takes part in.

    The page named the resource by its key and nothing else, so the hostname --
    the single most identifying fact about a DNS record -- appeared only inside
    a collapsed disclosure. And there was no way from here to the service page,
    which is where the rest of what serves that name lives and where the next
    thing is added.
    """

    provider = PROVIDERS[resource.kind]
    if provider.hostnames is None:
        return ()
    try:
        names = provider.hostnames(resolved_spec(resource))
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(
        (
            name,
            reverse("control_plane:service", kwargs={"hostname": name.lower().rstrip(".")}),
        )
        for name in names
        # A wildcard covers names rather than naming one, and there is no
        # service page for "*.example.com" to link to.
        if "*" not in name
    )


def _apply_note(kind: str) -> str:
    """What actually happens after saving, which is not the same for every kind.

    The form promised every resource would be applied at the provider within
    about a minute. That is true of most of them and false of any whose actions
    are locked -- a domain declaration records what HQ is responsible for and
    changes nothing, so the page was making a promise the capability registry
    already contradicted. The registry's own reason is the honest answer, and it
    is written once, there.
    """

    applies, explanation = controller_action_policy(
        kind, OperationRequest.Action.RECONCILE
    )
    if applies:
        return (
            "HQ applies this at the provider within about a minute, then shows "
            "you what it actually found there."
        )
    return explanation


def _readout_rows(resource) -> tuple[tuple[str, str, str], ...]:
    """``(label, desired, observed)`` as the provider describes itself.

    The same hook the service page and the domain page read, so one resource
    describes itself identically wherever it appears.
    """

    provider = PROVIDERS[resource.kind]
    if provider.readout is None:
        return ()
    try:
        return tuple(provider.readout(resource.spec, resource.status or {}))
    except (KeyError, TypeError, ValueError):
        return ()


def _removal_note(resource) -> str:
    """What this particular removal costs, if the provider says."""

    note = PROVIDERS[resource.kind].removal_note
    if note is None:
        return ""
    try:
        return note(resource.spec)
    except (KeyError, TypeError, ValueError):
        # A confirmation page that cannot render is worse than one missing a
        # sentence, and this is the page an operator uses to stop.
        return ""


def _after_save(request, kind: str, resource, saved: str) -> str:
    """Where saving lands: back at whatever this was being added to.

    Same rule as Cancel, and for the same reason -- the surface an operator came
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

    A form is reached from wherever the thing being edited appears -- a
    service, a domain, a list -- and returning to the resource instead put the
    operator somewhere they had not been, several clicks from the page they
    were working on. The origin is carried explicitly rather than guessed from
    Referer, which is absent, stale or forged often enough not to navigate by.

    Checked before use: an unvalidated redirect target taken from a query
    string is an open redirect regardless of how friendly the link looked.
    """

    return safe_next(request)


def _cancel_url(request, kind: str, resource) -> str:
    """Where "Cancel" belongs: back where the operator came from.

    The page that linked here wins, because it is the only one that actually
    knows. Failing that, editing returns to the resource and creating returns
    to the surface the provider says offered it -- so a record added from a
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
        # writes it in, which is how a derived value becomes an authored one --
        # the same adoption every record goes through, and equally a no-op the
        # first time.
        return {**_derived_spec(resource), **resource.spec}
    provider = PROVIDERS[kind]
    initial: dict = {}
    hostname = request.GET.get("hostname", "").strip()
    if provider.seed and hostname:
        initial.update(provider.seed(name_context(hostname)))
    for name in provider.spec_type.model_fields:
        value = request.GET.get(name, "").strip()
        if value:
            initial[name] = value
    return initial or None


def _form_context(request, resource) -> NameContext:
    """What HQ knows about the name this form is about.

    On create the name arrives in the query string, the same way every other
    seeded value does. On edit it is read back out of the resource through its
    own provider, because a form has to keep offering whatever the record
    already holds -- a menu that cannot describe an existing value tells the
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

    Asked for one, the form stopped to demand an identifier for a row in a table
    nobody had mentioned yet. The provider says what its own records should be
    called, and the same function answers here, at adoption, and during
    onboarding -- these disagreed once, and the form suggested a key built from
    a hostname the record did not have.
    """

    return suggest_key(kind, spec)


def _readable_error(exc) -> str:
    """What to show an operator when a use case refuses.

    Django collects several messages on one ValidationError, and str() of that
    renders the list with its brackets and quotes intact. Shared with the domain
    views, which had their own copy: two readers of the same exception would
    show the same refusal differently depending on which page you were on.
    """

    messages_found = getattr(exc, "messages", None)
    return " ".join(messages_found) if messages_found else str(exc)


class AdoptView(LoginRequiredMixin, View):
    """Bring something the provider already holds under HQ's management.

    One click, no form. The spec is read back out of the live record, so the
    declaration starts equal to the world and the first reconciliation changes
    nothing -- which is the only reason adopting is safe to do without asking
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
            f"Adopted {hostname} as {adopted}, exactly as configured now. "
            "Nothing changed at the provider.",
        )
        return redirect("control_plane:service", hostname=result["hostname"])


class CertificateUploadView(LoginRequiredMixin, View):
    """Take a certificate generated elsewhere and hold it for installation."""

    template_name = "control_plane/certificate_upload.html"

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
                    f"Stored a certificate covering {', '.join(stored['domains'])}. "
                    "HQ installs it on the next controller pass.",
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
            },
        )


class ResourceRemoveView(LoginRequiredMixin, View):
    """Ask first, then queue removal of the record itself.

    Not a row delete. What this describes lives at a provider, so dropping the
    declaration alone would leave the rewrite or proxy host in place with
    nothing in HQ pointing at it. HQ forgets its row only once a controller
    reports the provider is clear.
    """

    template_name = "control_plane/resource_confirm_remove.html"

    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        allowed, explanation = controller_action_policy(
            resource.kind, OperationRequest.Action.DELETE
        )
        if PROVIDERS[resource.kind].declaration_only:
            allowed, explanation = True, ""
        return render(
            request,
            self.template_name,
            {
                "resource": resource,
                "label": PROVIDERS[resource.kind].label or resource.kind,
                "removal_allowed": allowed,
                "removal_explanation": explanation,
                # Said by the provider, because what breaks depends on which
                # record this is: removing one of four CAA records is
                # housekeeping, and removing the last MX record stops the
                # domain receiving mail.
                "removal_note": _removal_note(resource),
                "spec_rows": _spec_rows(resource),
                "declaration_only": PROVIDERS[resource.kind].declaration_only,
            },
        )

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        try:
            result = request_removal(
                OperationCommand(
                    idempotency_key=f"web:{request.user.pk}:{uuid.uuid4()}",
                    reason=request.POST.get("reason", "").strip(),
                ),
                principal=web_principal(request.user),
                current_key=resource.key,
            )
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect("control_plane:detail", key=key)
        if "forgotten" in result:
            # Nothing was queued because nothing exists at the provider that HQ
            # made. Saying "queued removal" here would promise a deletion that
            # is neither happening nor wanted.
            released = len(result["released"])
            messages.success(
                request,
                f"HQ is no longer responsible for “{result['forgotten']}”"
                + (
                    f", and has released {released} record declaration"
                    f"{'' if released == 1 else 's'} in it"
                    if released
                    else ""
                )
                + ". Nothing changed at the provider.",
            )
            return redirect("control_plane:list")
        verb = "Queued" if result["queued"] else "Already queued"
        messages.success(
            request,
            f"{verb} removal of “{resource.key}”. HQ forgets it once the "
            "controller confirms the provider is clear.",
        )
        return redirect("control_plane:detail", key=key)


class ServiceListView(LoginRequiredMixin, TemplateView):
    """The hostname view of the same declarations the resource list shows."""

    template_name = "control_plane/service_list.html"

    def get_context_data(self, **kwargs):
        from application.pins import SERVICE, ordered
        from application.services import RUNTIME_FACET

        context = super().get_context_data(**kwargs)
        favorites = ordered(self.request.user, SERVICE)
        found = service_catalog(favorites)
        # Two tables rather than one with a rule through it. The few an
        # operator keeps at the top are a different list with a different
        # question -- "is my stuff healthy" against "what else is out there" --
        # and reordering only means anything within the first.
        context["favorites"] = [item for item in found if item.pinned]
        context["services"] = [item for item in found if not item.pinned]
        # Where a service runs is one column, not two. The runtime facet named
        # the container declaration and the origin named the machine it runs
        # on, side by side, in two different vocabularies for one fact.
        context["runtime_facet"] = RUNTIME_FACET
        # The column headers come from the providers, so a provider that
        # declares a new facet gets a column without this template being
        # touched -- and a facet no provider supplies yet gets none.
        context["facets"] = [
            facet for facet in service_facets() if facet[0] != RUNTIME_FACET
        ]
        # Everything the providers hold that no declaration accounts for. Shown
        # beside the managed services rather than on a page of its own: a
        # hostname HQ does not manage is still a hostname that is serving, and
        # hiding it is how a console ends up describing only the tidy half of
        # the estate.
        context["unmanaged"] = unmanaged_services()
        context["inventory"] = inventory_state()
        return context


class ServiceDetailView(LoginRequiredMixin, TemplateView):
    """One hostname, whether or not anything has been declared for it yet.

    A name with nothing behind it used to 404, which made this page unreachable
    at exactly the moment it is most useful: before anything exists. Publishing
    something therefore began at the resource picker, where an operator chose a
    kind of thing and typed a hostname, and only after saving did a page appear
    that knew what the name still needed -- so the second resource meant typing
    the name a second time.
    """

    template_name = "control_plane/service_detail.html"

    # Verbs, from the capability registry rather than from this template. A
    # controller that stops implementing one stops offering it here, and one
    # that gains a verb needs a route and a phrase -- not an edit to a page.
    LIFECYCLE = ("start", "stop", "restart")

    def get(self, request, *args, **kwargs):
        """An alias goes to the service it is an alias of.

        Rendered here, the page could only report that nothing supplied a name
        whose record is declared, healthy and listed on the domain page -- HQ
        contradicting itself about its own data.
        """

        canonical = alias_target(kwargs["hostname"])
        if canonical:
            return redirect("control_plane:service", hostname=canonical)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["service"] = service_or_prospect(self.kwargs["hostname"])
        context["container_kind"] = CONTAINER_KIND
        # Everything else HQ holds about this name, gathered by the name. Only
        # here: the board builds every service and needs none of it.
        context["sections"] = sections_for(context["service"])
        # Two filters, and both are readings rather than opinions: what the
        # controller says it implements for a container, and what the container's
        # current state makes worth asking. Offering all three always means
        # offering Start to something already running, whose only outcome is
        # Docker answering "already started" in a job result a minute later.
        container = context["service"].container
        offered = container.verbs if container else ()
        context["container_actions"] = tuple(
            (f"control_plane:{action}", action.capitalize())
            for action in self.LIFECYCLE
            if action in offered and controller_action_policy(CONTAINER_KIND, action)[0]
        )
        return context


class ServiceStartView(LoginRequiredMixin, View):
    """Ask for a hostname, then stand on its page.

    The whole of "publish a service" is knowing the name. Everything after it
    is already offered, seeded, by the page that name leads to.
    """

    def get(self, request):
        return render(request, "control_plane/service_start.html", {})

    def post(self, request):
        hostname = normalized_hostname(request.POST.get("hostname", ""))
        if not hostname or " " in hostname or "." not in hostname:
            messages.error(request, "Enter a hostname, like app.example.com.")
            return redirect("control_plane:service_start")
        return redirect("control_plane:service", hostname=hostname)


class MachineListView(LoginRequiredMixin, TemplateView):
    """Every machine anything reported, and what is on each.

    Nothing here is declared. A machine exists because a credential reaches it,
    a container runs on it, or a service is served from it -- so adding a VPS is
    registering it somewhere rather than entering it here.
    """

    template_name = "control_plane/machine_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["machines"] = machine_catalog()
        return context


def whatif_context(request, default: str = "") -> dict:
    """Everything the reachability panel needs, wherever it is included.

    One function because the panel appears on more than one page and the two
    must not drift into disagreeing about what they were asked. ``default``
    is the device a page is already about, so opening the panel there starts
    with the question most likely being asked.
    """

    from application.tailnet import (
        declaration,
        devices,
        may_reach,
        ports,
        proposed_grant,
    )

    known = devices()
    asked = {
        "source": request.GET.get("source", "") or default,
        "target": request.GET.get("target", "") or default,
        "port": request.GET.get("port", ""),
    }
    context = {
        "device_names": sorted(known),
        "ports": ports(),
        "asked": asked,
        "whatif_action": request.path,
        "policy_declaration": declaration(),
    }
    # Answered only when all three were given. A half-filled form is a question
    # nobody has asked yet, not one whose answer is "no".
    if asked["source"] and asked["target"] and asked["port"].isdigit():
        verdict = may_reach(asked["source"], asked["target"], int(asked["port"]))
        context["verdict"] = verdict
        # A refusal is the moment somebody wants to change the policy, so the
        # grant that would allow it is worked out here rather than left as an
        # exercise. Shown, never applied: what to do about a denial is the
        # operator's call and the editor is where it is made.
        if verdict.known and not verdict.allowed:
            context["proposal"] = proposed_grant(
                asked["source"], asked["target"], int(asked["port"])
            )
    return context


class TailnetView(LoginRequiredMixin, TemplateView):
    """The tailnet, and whether one machine may reach another.

    A page because the question has nowhere else to live. Reachability is not a
    property of any one declaration -- it is the policy's answer about a pair --
    so it belongs beside the devices rather than on any of them.
    """

    template_name = "control_plane/tailnet.html"

    def get_context_data(self, **kwargs):
        from application.tailnet import policy

        context = super().get_context_data(**kwargs)
        context.update(whatif_context(self.request))
        context["policy"] = policy()
        return context


class MachineDetailView(LoginRequiredMixin, TemplateView):
    """One machine, and everything that ties to it.

    The page exists because the ties did and had nowhere to meet: a container's
    host, a proxy's forwarding address, what a Portainer says it reaches and
    which credential opens a shell there were four facts about one thing, on
    four screens, joined by an operator's memory.
    """

    template_name = "control_plane/machine_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        found = machine(self.kwargs["name"])
        if found is None:
            raise Http404("No machine of that name has been reported.")
        context["machine"] = found
        context["container_kind"] = CONTAINER_KIND
        # The same panel as the tailnet page, started on this machine. Asked
        # here it is nearly always about this one, so both ends default to it
        # and changing either is one dropdown rather than three.
        found = context.get("machine")
        context.update(
            whatif_context(
                self.request,
                default=found.presence.tailnet_name if found and found.presence else "",
            )
        )
        return context


class ConnectionListView(LoginRequiredMixin, TemplateView):
    """What HQ can reach, as the controllers last found it.

    Read-only by construction. Every row here started as a 1Password item, and
    the only way to change one is to change that item -- so this page reports
    and never edits, which is what keeps it from becoming a second inventory.
    """

    template_name = "control_plane/connection_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        readings = connection_readings()
        context["connections"] = readings
        context["unlabelled"] = [item for item in readings if not item.provider]
        # The oldest of them, because the page's honesty depends on the staler
        # half: reporting the newest would describe a controller that is still
        # sweeping as though every row were current.
        context["observed_at"] = min(
            (item.observed_at for item in readings), default=None
        )
        # Which controller reported a connection only matters once there are
        # two. Printed unconditionally it repeated one machine's name under
        # every row, which on a phone was a third of the page saying nothing.
        context["name_the_controller"] = (
            len({item.controller_id for item in readings}) > 1
        )
        return context


class InfrastructureListView(LoginRequiredMixin, ListView):
    model = ManagedResource
    template_name = "control_plane/resource_list.html"
    context_object_name = "resources"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for resource in context["resources"]:
            resource.control_health = resource_health(resource)
            # What it is, in the provider's own words. A list of keys alone
            # said "jseverino-com-caa-2" twenty times over -- names HQ invented,
            # each describing nothing.
            rows = _readout_rows(resource)
            resource.summary = rows[0][1] or rows[0][2] if rows else ""
            # Nothing to converge, so nothing is ever pending. A domain records
            # a responsibility and has no controller action at all; reported as
            # "Pending" and "Never observed" it described a resource waiting
            # forever for something that is never coming.
            resource.declaration_only = PROVIDERS[resource.kind].declaration_only
        context["operations"] = OperationRequest.objects.select_related("resource")[:12]
        context["provider_catalog"] = describe_providers()
        return context


class InfrastructureDetailView(LoginRequiredMixin, DetailView):
    model = ManagedResource
    slug_field = "key"
    slug_url_kwarg = "key"
    template_name = "control_plane/resource_detail.html"
    context_object_name = "resource"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        allowed, explanation = certificate_renewal_allowed(self.object)
        context["renewal_allowed"] = allowed
        context["renewal_explanation"] = explanation
        context["control_health"] = resource_health(self.object)
        reconcile_allowed, reconcile_explanation = controller_action_policy(
            self.object.kind, OperationRequest.Action.RECONCILE
        )
        context["reconcile_allowed"] = reconcile_allowed
        context["reconcile_explanation"] = reconcile_explanation
        context["controller_capability"] = describe_providers()["controller"][
            "capabilities"
        ][self.object.kind]["actions"]
        context["declaration_only"] = PROVIDERS[self.object.kind].declaration_only
        context["sync_state"] = (
            "in_sync"
            if self.object.generation == self.object.observed_generation
            else "pending"
        )
        # What this resource does, said by its own provider. The page used to
        # carry a hand-written card per kind, reaching into spec.forward_host
        # and spec.answer -- the one thing nothing outside a provider is allowed
        # to do, and the reason a provider added later had no detail card at all.
        context["label"] = PROVIDERS[self.object.kind].label or self.object.kind
        context["service_links"] = _service_links(self.object)
        # Where this resource sends traffic, when it sends it anywhere. A
        # provider that declares an origin is one whose thing runs on a machine,
        # so the machine is a link rather than an address printed in a readout.
        context["origin_machine"] = _origin_machine(self.object)
        # A container declares identity and nothing else, so everything worth
        # opening the page for is a join: which machine that name is, what the
        # container is doing, and which services reach it through the ports it
        # publishes. All three come from the sweep this resource was adopted out
        # of, so none of it is a second opinion about anything.
        if self.object.kind == CONTAINER_KIND:
            context["container"] = container_context(
                self.object.spec.get("host", ""), self.object.spec.get("name", "")
            )
        # Lifecycle verbs the controller implements for this kind, narrowed to
        # the ones the thing's own state makes worth asking. Read from the
        # registry rather than listed here, so a verb a controller gains needs a
        # route and a phrase and nothing else.
        running = (context.get("container") or {}).get("running")
        offered = running.verbs if running else ()
        context["lifecycle_actions"] = tuple(
            (f"control_plane:{action}", action.capitalize())
            for action in ServiceDetailView.LIFECYCLE
            if action in offered
            and controller_action_policy(self.object.kind, action)[0]
        )
        # A resource with a removal in flight is on its way out. Offering Edit
        # and Reconcile unchanged invited an operator to work on something that
        # is about to stop existing, and to queue a convergence that races the
        # deletion already waiting for the same controller.
        context["removal_pending"] = self.object.operations.filter(
            action=OperationRequest.Action.DELETE,
            state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED),
        ).exists()
        context["readout_rows"] = _readout_rows(self.object)
        context["spec_rows"] = _spec_rows(self.object)
        context["days_left"] = None
        context["renewal_at"] = None
        not_after = self.object.status.get("not_after")
        if not_after:
            try:
                expiry = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                context["days_left"] = max(
                    0,
                    math.ceil(
                        (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
                    ),
                )
                context["renewal_at"] = expiry - timedelta(
                    days=self.object.spec.get("renewal_window_days", 30)
                )
            except (TypeError, ValueError):
                # A malformed provider timestamp must not break the resource page.
                pass
        context["operations"] = [
            operation_summary(operation) for operation in self.object.operations.all()[:20]
        ]
        for operation in context["operations"]:
            operation["created_at"] = datetime.fromisoformat(operation["created_at"])
            if operation["completed_at"]:
                operation["completed_at"] = datetime.fromisoformat(
                    operation["completed_at"]
                )
        context["resolved_spec"] = self.object.spec
        if self.object.kind == CERTIFICATE_KIND:
            try:
                context["resolved_spec"] = controller_contract(self.object)["resource"]["spec"]
                context["resolution_error"] = ""
                observed_names: dict[str, set[str]] = {}
                for observation in self.object.status.get("consumers", []):
                    observed_names.setdefault(observation.get("consumer", ""), set()).add(
                        observation.get("domain", "")
                    )
                # The target each consumer came from, so the page that shows
                # where a certificate goes links to where those settings are
                # changed rather than making the operator find it by name.
                targets = {
                    resource.spec.get("connection_ref"): resource.key
                    for resource in ManagedResource.objects.filter(
                        kind=DELIVERY_TARGET_KIND, enabled=True
                    )
                }
                context["display_consumers"] = [
                    {
                        **consumer,
                        "url": (
                            reverse(
                                "control_plane:detail",
                                kwargs={"key": targets[consumer["connection_ref"]]},
                            )
                            if consumer.get("connection_ref") in targets
                            else ""
                        ),
                        "display_domains": sorted(
                            domain
                            for domain in observed_names.get(consumer["name"], set())
                            if domain
                        )
                        or consumer.get("verify_domains", []),
                    }
                    for consumer in context["resolved_spec"]["consumers"]
                ]
            except (KeyError, ValueError) as exc:
                context["resolution_error"] = str(exc)
        context["diagnostic_status"] = serialize_public_status(self.object.status)
        return context


# What each controller verb is called in a sentence. One entry per verb, in one
# place, because the alternative was one view class per verb: "reconcile" and
# "renew" had a class each, identical but for this word, and the next verb the
# credential allows -- purging a cache, rotating a key -- would have been a
# third copy of the same eleven lines.
OPERATION_PHRASE = {
    OperationRequest.Action.RECONCILE: "reconciliation",
    OperationRequest.Action.RENEW: "certificate renewal",
    OperationRequest.Action.DELETE: "removal",
    OperationRequest.Action.RESTART: "a restart",
    OperationRequest.Action.START: "a start",
    OperationRequest.Action.STOP: "a stop",
}


class AdoptRecordView(LoginRequiredMixin, View):
    """Take on one record a sweep found, identified by what makes it that record.

    Separate from adopting a service because a service is a hostname and some
    records have none: a container answers wherever its ports are pointed, so
    there is no name to route by and no group of records to take on together.

    The spec is read back out of the sweep rather than posted, the same as every
    other adoption -- the declaration starts equal to the world, so the first
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
                f"HQ is now watching “{result['resource']['key']}”, exactly as "
                "it is running.",
            )
        return redirect(safe_next(request, fallback=reverse("control_plane:list")))


class OperationView(LoginRequiredMixin, View):
    """Ask the controller for one action on one resource.

    The action comes from the URL rather than from the class, so adding a verb
    is a route and a phrase rather than another view that does what this one
    already does.
    """

    action = OperationRequest.Action.RECONCILE

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        try:
            result = _web_operation(request, resource, self.action)
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect("control_plane:detail", key=key)
        verb = "Queued" if result["queued"] else "Already queued"
        phrase = OPERATION_PHRASE.get(self.action, self.action)
        messages.success(request, f"{verb} {phrase} for “{resource.key}”.")
        return redirect("control_plane:detail", key=key)


class CertificateDownloadView(LoginRequiredMixin, View):
    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        certificate_pem = resource.status.get("certificate_pem", "")
        if resource.kind != CERTIFICATE_KIND or not certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "No verified public certificate is available."},
                status=404,
            )
        if "PRIVATE KEY-----" in certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "Unsafe certificate artifact rejected."},
                status=500,
            )
        response = HttpResponse(certificate_pem, content_type="application/x-pem-file")
        response["Content-Disposition"] = (
            f'attachment; filename="{resource.key}-public.pem"'
        )
        return response


class ResourceReportDownloadView(LoginRequiredMixin, View):
    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        payload = {
            "schema_version": 1,
            "resource": serialize_resource(resource),
            "operations": [
                {
                    "id": str(operation.id),
                    "action": operation.action,
                    "state": operation.state,
                    "created_at": operation.created_at.isoformat(),
                    "result": operation.result,
                }
                for operation in resource.operations.all()[:50]
            ],
        }
        response = JsonResponse(payload, json_dumps_params={"indent": 2})
        response["Content-Disposition"] = (
            f'attachment; filename="{resource.key}-status.json"'
        )
        return response


class ProviderSchemaView(LoginRequiredMixin, View):
    def get(self, request):
        return JsonResponse(describe_providers(), json_dumps_params={"indent": 2})


class ServicePinView(LoginRequiredMixin, View):
    """Keep a service at the top of the list, for this operator only.

    A preference, so it never touches a spec: starring a hostname does not
    bump a generation, queue a reconcile, or change anything about the world.
    """

    def post(self, request, hostname: str):
        from application.pins import SERVICE, toggle

        name = normalized_hostname(hostname)
        if not name:
            raise Http404("No such service.")
        toggle(request.user, SERVICE, name)
        return redirect(safe_next(request) or reverse("control_plane:services"))


class ServiceMoveView(LoginRequiredMixin, View):
    """Move one favorite past its neighbour.

    Up and down rather than dragging: it is one POST, it works without script,
    and it says out loud which two things swapped -- which a drag does not.
    """

    def post(self, request, hostname: str):
        from application.pins import SERVICE, move

        name = normalized_hostname(hostname)
        if not name:
            raise Http404("No such service.")
        delta = -1 if request.POST.get("direction") == "up" else 1
        move(request.user, SERVICE, name, delta)
        return redirect(safe_next(request) or reverse("control_plane:services"))
