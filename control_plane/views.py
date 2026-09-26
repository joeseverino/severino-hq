from __future__ import annotations

import uuid
import math
from datetime import datetime, timedelta, timezone
from functools import cached_property
from typing import Any, get_origin
from urllib.parse import urlencode

from django.contrib import messages
from contextlib import suppress

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
    controller_contract,
    declared_machines,
    delivery_targets,
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
from application.glance import dashboard_machine_selected, select_dashboard_machine
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
from application.connections import CONTROLLER_CONNECTIONS, connection_catalog, machines_once
from application.machine_context import machine_links
from application.entity_links import NODE_KINDS, entity_link, kind_label, node_link
from application.relationships import relationships_for
from application.credential_sight import sight_by_connection
from application.connection_security import (
    connection_security_posture,
    observed_connection_controls,
)
from application.analytics import HOST_TRAFFIC_DAYS
from application.action_links import topology_url
from application.findings import derive_findings, finding_rules, rule_for
from application.topology import (
    RELATIONS,
    relation_rank,
    apply_lens,
    apply_trace,
    derive_topology,
    lens_for,
    observable,
    topology_lenses,
)
from application.hq_self import LABEL as HQ_LABEL, hq_service
from application.machines import (
    container_context,
    declaration_seed,
    machine,
)
from application.services import RUNTIME_FACET, machine_link, whereabouts
from application.naming import name_context
from application.plugins import _import
from application.provider_forms import (
    CertificateUploadForm,
    ResourceIdentityForm,
    spec_form_class,
)
from application.security import AuthorizationError, safe_next, web_principal
from application.machine_context import sections_for as machine_sections
from application.service_context import sections_for
from application.pages import PageAction, PageMixin, page_context
from application.resource_capabilities import (
    LIFECYCLE_VERBS,
    VERB_LABELS,
    kind_converges,
    resource_capabilities,
)
from application.ui import PageNavigation, PageSection, counted
from application.services import (
    CONTAINER_KIND,
    alias_target,
    service_catalog,
    service_or_prospect,
)

from core import secrets
from core.audit import last_activity
from core.templatetags.nav_tags import returning_to

from .models import ManagedResource, OperationRequest
from .providers import (
    CERTIFICATE_KIND,
    DELIVERY_TARGET_KIND,
    MACHINE_KIND,
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
                # that exists. A certificate menu with one entry was right by
                # luck; the second certificate is what makes it a question.
                "spec": spec,
                # Collected here rather than on a page of its own. A resource
                # that is not usable without material should not be creatable
                # without it.
                "material": material,
                "cancel_url": _cancel_url(request, kind, resource),
                "apply_note": _apply_note(kind),
                # What this resource already is, when editing one. A form whose
                # fields are mostly derived elsewhere shows empty boxes and
                # nothing else: an edit page for a certificate said nothing
                # about the names it covers or where it is installed, which is
                # the whole of what a person came to check.
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
            try:
                result = save_managed_resource(
                    ManagedResourceCommand(
                        # The identifier is never asked for again once a
                        # resource exists, so an edit keeps the one it has.
                        key=(
                            resource.key if resource else _derived_key(kind, spec.spec)
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
                # own page after saving it made the next step a navigation
                # problem: the service page is the thing being built.
                return redirect(_after_save(request, kind, resource, saved))
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


def _form_page(kind: str, resource) -> dict:
    """The head of the resource form, the same on a first render and a retry."""

    label = kind_label(kind)
    return page_context(
        f"Edit {resource.key}" if resource else f"Add {label}",
        PROVIDERS[kind].summary,
    )


def _spec_value(value: Any) -> str:
    """One spec field as a person reads it: a list as its items, not its repr."""

    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _spec_rows(resource) -> dict[str, tuple[tuple[str, str], ...]]:
    """A spec as an operator reads it, split the way the form splits it.

    Shown before a destructive action, so:

    - Fields by their titles from the model, never their names.
    - An unset optional is left out rather than shown as "None".
    - Fields the provider declares routine fold away, the same split the form
      makes.
    """

    provider = PROVIDERS[resource.kind]
    fields = provider.spec_type.model_fields
    # What the readout above already printed. On anything with a handful of
    # fields the readout *is* the spec, so the disclosure repeated it whole,
    # a machine showed "What it is for" and its addresses, then offered "every
    # field of this declaration" and showed the same two again with the name.
    shown = {str(label).strip().casefold() for label, _, _ in _readout_rows(resource)}
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
        if label.strip().casefold() in shown:
            continue
        rendered = _spec_value(value)
        # And not the thing the page is already titled. A machine's name is its
        # identifier here, so the last row left after the readout was the
        # heading repeated inside a disclosure offering more.
        #
        # Only when they are the same string: a declaration whose name differs
        # from its filing is telling you something, and that is the case worth
        # showing.
        if rendered == resource.key:
            continue
        row = (label, rendered)
        (advanced if name in provider.advanced_fields else primary).append(row)
    return {"primary": tuple(primary), "advanced": tuple(advanced)}


def _origin_machine(resource, machines=None, at=None, targets=None):
    """The machine a resource forwards to, if its provider says where it serves.

    The readings are passed in by a page asking this of every row, where taking
    them here is the same four queries repeated once per resource.
    """

    provider = PROVIDERS.get(resource.kind)
    if provider is None or provider.origin is None:
        return None
    try:
        origin = provider.origin(resolved_spec(resource, targets))
    except (KeyError, TypeError, ValueError):
        return None
    return machine_link(origin, machines, at) if origin else None


def _provider_machine(resource):
    """The one machine hosting the provider that manages this resource.

    The resource's origin is where it sends traffic. The provider connection
    is where the proxy, DNS server, or controller itself runs. Conflating those
    two edges made an NPM proxy look as though it ran on its upstream service.
    """

    from application.connections import connection_readings
    from application.machines import machine as machine_named

    provider = PROVIDERS.get(resource.kind)
    if provider is None:
        return None
    matches = {
        found.name
        for reading in connection_readings()
        if reading.provider in provider.connection_providers
        if (found := machine_named(reading.controller_id)) is not None
    }
    if len(matches) != 1:
        return None
    link = entity_link("machine", matches.pop())
    return {"name": link.label, "url": link.url, "link": link}


def _service_links(resource) -> tuple[tuple[str, str], ...]:
    """``(hostname, url)`` for every service this resource takes part in.

    The hostname is the most identifying fact about a DNS record, and its
    service page is where the rest of what serves that name lives.
    """

    provider = PROVIDERS[resource.kind]
    if provider.hostnames is None:
        return ()
    try:
        names = provider.hostnames(resolved_spec(resource))
    except (KeyError, TypeError, ValueError):
        return ()
    links = ((name, entity_link("service", name).url) for name in names)
    return tuple((name, url) for name, url in links if url)


def _apply_note(kind: str) -> str:
    """What actually happens after saving, which is not the same for every kind.

    The form promised every resource would be applied at the provider within
    about a minute. That is true of most of them and false of any whose actions
    are locked: a domain declaration records what HQ is responsible for and
    changes nothing, so the page was making a promise the capability registry
    already contradicted. The registry's own reason is the honest answer, and it
    is written once, there.
    """

    applies, explanation = controller_action_policy(
        kind, OperationRequest.Action.RECONCILE
    )
    if applies:
        return "Applies at the provider within about a minute."
    return explanation


def _linked_readout(resource, relationships) -> tuple[tuple[str, str, str, tuple], ...]:
    """The readout, with each value that names a related entity as its link.

    A blank connection field is filled from the connections the relation graph
    says use this declaration.
    """

    related = {
        item.entity.label: item.entity
        for group in relationships.groups
        for item in group.items
    }
    connections = tuple(
        item.entity
        for group in relationships.groups
        for item in group.items
        if item.entity.kind == "connection"
    )
    field = PROVIDERS[resource.kind].spec_type.model_fields.get("connection_ref")
    connection_label = (field.title or "") if field is not None else ""
    rows = []
    for label, desired, observed in _readout_rows(resource):
        value = str(observed or desired or "")
        if value in related:
            links = (related[value],)
        elif not value and connection_label and label == connection_label:
            links = connections
        else:
            links = ()
        rows.append((label, desired, observed, links))
    return tuple(rows)


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


def _form_facts(resource, form) -> tuple[tuple[str, str, str], ...]:
    """What this resource is that the form below does not already ask.

    The panel exists for the fields a form cannot show: a certificate's edit
    page asks which target it installs on and says nothing about the names it
    covers or what is actually served with it, which is the whole of what a
    person came to check.

    On a machine it had the opposite problem. Every field on that form is on
    that form, so the panel repeated them: "What it is for" twice and the
    addresses twice, once as text and once as inputs, on one screen. Matched by
    label against the form's own fields, a row survives only when nothing below
    is asking about it.

    The identifier leads, and is the reason this is not empty for a machine.

    Added here rather than in the readout, which every list and detail surface
    shares: they identify the resource in their own way already, and a row
    repeating it on each of them is the same fact three times.
    """

    asked = {str(field.label).strip().casefold() for field in form}
    return (("Identifier", "", resource.key),) + tuple(
        row
        for row in _readout_rows(resource)
        if str(row[0]).strip().casefold() not in asked
    )


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
    service, a domain, a list) and returning to the resource instead put the
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
    views, which had their own copy: two readers of the same exception would
    show the same refusal differently depending on which page you were on.
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
        capabilities = resource_capabilities(resource)
        forget = capabilities.removal == "forget"
        return render(
            request,
            self.template_name,
            {
                "resource": resource,
                "label": kind_label(resource.kind),
                "removal_allowed": capabilities.removal != "unavailable",
                "removal_explanation": capabilities.removal_reason,
                # Said by the provider, because what breaks depends on which
                # record this is: removing one of four CAA records is
                # housekeeping, and removing the last MX record stops the
                # domain receiving mail.
                "removal_note": _removal_note(resource),
                "spec_rows": _spec_rows(resource),
                "forget": forget,
                "holds_records": bool(PROVIDERS[resource.kind].contains),
                "confirm": {
                    "url": reverse("control_plane:remove", args=[resource.key]),
                    "label": "Stop managing" if forget else "Remove",
                    "cancel_url": resource.get_absolute_url(),
                },
                **page_context(
                    f"Remove {resource.key}?",
                    kind_label(resource.kind),
                ),
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
                f"Stopped managing “{result['forgotten']}”"
                + (
                    f" and {counted(released, 'record declaration', 'record declarations')} in it"
                    if released
                    else ""
                )
                + ". Nothing changed at the provider.",
            )
            return redirect("control_plane:list")
        verb = "Queued" if result["queued"] else "Already queued"
        messages.success(
            request,
            f"{verb} removal of “{resource.key}”. HQ drops it once the "
            "provider is clear.",
        )
        return redirect("control_plane:detail", key=key)


class ServiceListView(PageMixin, LoginRequiredMixin, TemplateView):
    """The hostname view of the same declarations the resource list shows."""

    template_name = "control_plane/service_list.html"
    page_title = "Services"
    page_lede = "Hostnames HQ manages, with their DNS, ingress and certificate."

    def get_page_actions(self):
        # "Add" on the services board starts a service, not the resource picker.
        return (
            PageAction(
                "Publish a service",
                reverse("control_plane:service_start"),
                primary=True,
            ),
        )

    def get_context_data(self, **kwargs):
        from application.pins import SERVICE, ordered
        from application.services import RUNTIME_FACET

        context = super().get_context_data(**kwargs)
        favorites = ordered(self.request.user, SERVICE)
        found = service_catalog(favorites)
        # Two tables rather than one with a rule through it. The few an
        # operator keeps at the top are a different list with a different
        # question: "is my stuff healthy" against "what else is out there",
        # and reordering only means anything within the first.
        context["favorites"] = [item for item in found if item.pinned]
        context["services"] = [item for item in found if not item.pinned]
        # One answer for both tables: asked separately, the two halves of one
        # catalogue could render different columns.
        context["service_projects"] = any(item.project for item in found)
        # Where a service runs is one column, not two. The runtime facet named
        # the container declaration and the origin named the machine it runs
        # on, side by side, in two different vocabularies for one fact.
        context["runtime_facet"] = RUNTIME_FACET
        # The column headers come from the providers, so a provider that
        # declares a new facet gets a column without this template being
        # touched, and a facet no provider supplies yet gets none.
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
        # HQ itself: derived, listed, and never reconciled.
        context["hq_service"] = hq_service(self.request)
        return context


class ServiceDetailView(PageMixin, LoginRequiredMixin, TemplateView):
    """One hostname, whether or not anything has been declared for it yet.

    A name with nothing behind it still has a page: it is where publishing a
    service starts.
    """

    template_name = "control_plane/service_detail.html"

    def get(self, request, *args, **kwargs):
        """An alias goes to the service it is an alias of.

        Rendered here, the page could only report that nothing supplied a name
        whose record is declared, healthy and listed on the domain page: HQ
        contradicting itself about its own data.
        """

        if not entity_link("service", kwargs["hostname"]).url:
            raise Http404("Not a host name.")
        canonical = alias_target(kwargs["hostname"])
        if canonical:
            return redirect("control_plane:service", hostname=canonical)
        return super().get(request, *args, **kwargs)

    @cached_property
    def service(self):
        return service_or_prospect(self.kwargs["hostname"])

    @cached_property
    def own(self):
        """HQ's own service, when this page is about it. Derived and read-only."""

        own = hq_service(catalog=machines_once())
        if own is not None and self.service.hostname in own.hostnames:
            return own
        return None

    @cached_property
    def relationships(self):
        return relationships_for(
            f"service:{self.service.hostname}",
            principal=web_principal(self.request.user),
        )

    @cached_property
    def sections(self):
        # Everything else HQ holds about this name, gathered by the name. Only
        # here: the board builds every service and needs none of it.
        return sections_for(self.service)

    def get_page_title(self):
        return self.service.hostname

    def get_page_trail(self):
        return (("Services", reverse("control_plane:services")),)

    def get_page_navigation(self):
        return PageNavigation(
            (
                PageSection("overview", "Overview"),
                *(PageSection(section.id, section.label) for section in self.sections),
                *(
                    (PageSection("relationships", "Relationships"),)
                    if self.relationships
                    else ()
                ),
                *(
                    (PageSection("resources", "Resources"),)
                    if self.service.claims or self.service.alias_claims
                    else ()
                ),
            )
        )

    def get_page_actions(self):
        """The container's verbs, then the way to the domain.

        In the page head rather than the card that describes the container.
        Which verbs appear is the watching declaration's capabilities, given
        the container's current state.
        """

        if self.own is not None:
            # HQ does not act on itself from its own page.
            return ()
        container = self.service.container
        actions = []
        watcher = (
            ManagedResource.objects.filter(key=container.watcher).first()
            if container and container.watcher
            else None
        )
        if watcher is not None:
            capabilities = resource_capabilities(watcher, running=container.verbs)
            actions.extend(
                PageAction(
                    VERB_LABELS[verb],
                    reverse(f"control_plane:{verb}", kwargs={"key": watcher.key}),
                    method="post",
                    disabled=not allowed.enabled,
                    title=allowed.reason or f"{VERB_LABELS[verb]} {container.name}",
                )
                for verb, allowed in capabilities.page_actions
                if verb in LIFECYCLE_VERBS
            )
        elif container:
            actions.append(
                PageAction(
                    "Watch container",
                    reverse(
                        "control_plane:adopt_record",
                        kwargs={"kind": CONTAINER_KIND, "token": container.token},
                    ),
                    method="post",
                    title=f"Let HQ start, stop and restart {container.name}",
                )
            )
        if self.service.zone_key:
            actions.append(
                PageAction("Domain", reverse("zones:detail", args=[self.service.zone_key]))
            )
        return tuple(actions)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["service"] = self.service
        context["container_kind"] = CONTAINER_KIND
        context["sections"] = self.sections
        context["relationships"] = self.relationships
        context["read_only"] = self.own is not None
        context["hq_label"] = HQ_LABEL
        context["runtime_facet"] = RUNTIME_FACET
        context["hq_machine"] = (
            entity_link("machine", self.own.machine) if self.own and self.own.machine else None
        )
        return context


class ServiceStartView(LoginRequiredMixin, View):
    """Ask for a hostname, then stand on its page.

    The whole of "publish a service" is knowing the name. Everything after it
    is already offered, seeded, by the page that name leads to.
    """

    def get(self, request):
        return render(
            request,
            "control_plane/service_start.html",
            page_context("Publish a service", "Enter a hostname to see what it needs."),
        )

    def post(self, request):
        hostname = normalized_hostname(request.POST.get("hostname", ""))
        if not hostname or " " in hostname or "." not in hostname:
            messages.error(request, "Enter a hostname, like app.example.com.")
            return redirect("control_plane:service_start")
        return redirect("control_plane:service", hostname=hostname)


class MachineListView(PageMixin, LoginRequiredMixin, TemplateView):
    """Every machine anything reported, and what is on each.

    Nothing here is declared. A machine exists because a credential reaches it,
    a container runs on it, or a service is served from it, so adding a VPS is
    registering it somewhere rather than entering it here.
    """

    template_name = "control_plane/machine_list.html"
    page_title = "Machines"
    page_lede = "Machines HQ can reach or has seen running something."

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["machines"] = machines_once()
        context["hq_label"] = HQ_LABEL
        return context


def whatif_context(request, default: str = "", *, target_default: str | None = None) -> dict:
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
        "target": request.GET.get("target", "")
        or (default if target_default is None else target_default),
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


class TailnetView(PageMixin, LoginRequiredMixin, TemplateView):
    """The tailnet, and whether one machine may reach another.

    A page because the question has nowhere else to live. Reachability is not a
    property of any one declaration: it is the policy's answer about a pair,
    so it belongs beside the devices rather than on any of them.
    """

    template_name = "control_plane/tailnet.html"
    page_title = "Tailnet"
    page_lede = "The tailnet access policy. Devices are on the Machines page."

    def get_page_actions(self):
        from application.tailnet import declaration

        policy_declaration = declaration()
        return (
            PageAction(
                "Policy test",
                reverse("control_plane:tailnet"),
                primary=True,
                modal="whatif",
            ),
            *(
                (
                    PageAction(
                        "Edit policy",
                        reverse("control_plane:edit", args=[policy_declaration]),
                    ),
                )
                if policy_declaration
                else ()
            ),
            PageAction("All machines", reverse("control_plane:machines")),
        )

    def get_context_data(self, **kwargs):
        from dataclasses import replace

        from application.policy_links import PolicyNames, tagged
        from application.tailnet import RESOLVES_THROUGH, devices, grant_ports, policy

        context = super().get_context_data(**kwargs)
        context.update(whatif_context(self.request))
        found = policy()
        found = replace(found, grants=grant_ports(found.grants))
        context["policy"] = found
        # Every name the policy writes, as the machine it stands for.
        names = PolicyNames(hosts=found.hosts)
        context["grant_rows"] = tuple(
            (grant, names.of(grant.get("src") or ()), names.of(grant.get("dst") or ()))
            for grant in found.grants
        )
        context["ssh_rows"] = tuple(
            (rule, names.of(rule.get("src") or ()), names.of(rule.get("dst") or ()))
            for rule in found.ssh_rules
        )
        context["fact_rows"] = tuple(
            (label, names.addresses(value.split(", ")) if label == RESOLVES_THROUGH else None, value)
            for label, value in found.facts
        )
        context["tag_devices"] = tagged(devices(), names)
        return context


class MachineDetailView(PageMixin, LoginRequiredMixin, TemplateView):
    """One machine, and everything that ties to it.

    The page exists because the ties did and had nowhere to meet: a container's
    host, a proxy's forwarding address, what a Portainer says it reaches and
    which credential opens a shell there were four facts about one thing, on
    four screens, joined by an operator's memory.
    """

    template_name = "control_plane/machine_detail.html"

    def get(self, request, *args, **kwargs):
        self.found = machine(kwargs["name"])
        if self.found is None:
            raise Http404("No machine of that name has been reported.")
        if self.found.name.lower() != kwargs["name"].strip().lower():
            # Asked for by another of its names; the page lives at one.
            return redirect("control_plane:machine", name=self.found.name)
        return super().get(request, *args, **kwargs)

    def get_page_title(self):
        return self.found.name

    def get_page_trail(self):
        return (("Machines", reverse("control_plane:machines")),)

    def get_page_actions(self):
        # The machine's declaration is edited from its own page.
        if self.found.declaration:
            key = self.found.declaration
            return tuple(
                [
                    PageAction("Edit machine", reverse("control_plane:edit", args=[key])),
                    PageAction(
                        "Remove", reverse("control_plane:remove", args=[key]), danger=True
                    ),
                ]
            )
        # Seeded with what HQ knows, and back here after saving. A machine the
        # tailnet device declaration already names gets its details added; one
        # with no declaration at all is declared.
        seeded = urlencode(
            {
                "kind": MACHINE_KIND,
                **declaration_seed(self.found),
                "next": self.found.url,
            },
            doseq=True,
        )
        return (
            PageAction(
                "Add machine details" if self.found.route_approval_key else "Declare machine",
                f"{reverse('control_plane:create')}?{seeded}",
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        found = self.found
        context["machine"] = found
        device = (
            ManagedResource.objects.filter(key=found.route_approval_key).first()
            if found.route_approval_key
            else None
        )
        context["route_approval"] = (
            resource_capabilities(device, running=()).actions.get("approve-routes")
            if device is not None
            else None
        )
        context["route_approval_off"] = bool(
            context["route_approval"] and not context["route_approval"].enabled
        )
        # What else HQ can say about this machine, from a registry rather than
        # from this view. A band appears because a resolver produced one, so
        # what HQ learns next reaches the page without either being edited.
        context["sections"] = machine_sections(found)
        relationships = relationships_for(
            f"machine:{found.name}", principal=web_principal(self.request.user)
        )
        # "Declared as" above names the tailnet device declaration with its kind.
        if found.route_approval_key:
            relationships = relationships.without(RELATIONS["on_tailnet"].phrase)
        context["relationships"] = relationships
        context.update(machine_links(found))
        # Whether you are reading this on the machine it describes. HQ already
        # judged the caller's address for the network gate, and every machine
        # carries the addresses it answers at, so the page could always have
        # known, and said "this machine" while you looked at your own laptop.
        # Arithmetic on one address: no query, no sweep.
        from application.connection import displayed_client_ip

        context["is_this_device"] = displayed_client_ip(self.request) in found.addresses
        context["hq_label"] = HQ_LABEL
        context["container_kind"] = CONTAINER_KIND
        from application.machine_context import header_addresses

        context["header_addresses"] = header_addresses(found)
        # The same panel as the tailnet page, started on this machine. Asked
        # here it is nearly always about this one, so both ends default to it
        # and changing either is one dropdown rather than three.
        # From this device to the machine HQ runs on, which is the question a
        # machine page is usually asked. On HQ's own machine the target is left
        # for the operator to choose.
        own = found.presence.tailnet_name if found.presence else ""
        hq = next((item for item in machines_once() if item.runs_hq), None)
        hq_device = hq.presence.tailnet_name if hq and hq.presence else ""
        context.update(
            whatif_context(
                self.request,
                default=own,
                target_default="" if hq_device == own else hq_device,
            )
        )
        return context


class ConnectionListView(PageMixin, LoginRequiredMixin, TemplateView):
    """What HQ can reach, as the controllers last found it.

    Read-only by construction. Every row here started as a 1Password item, and
    the only way to change one is to change that item, so this page reports
    and never edits, which is what keeps it from becoming a second inventory.
    """

    template_name = "control_plane/connection_list.html"
    page_title = "Connections"
    page_lede = "What HQ connects to and what each connection can do."

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        groups = connection_catalog(principal=web_principal(self.request.user))
        context["connection_groups"] = groups
        context["unconfigured_groups"] = [group for group in groups if not group.connections]
        core = next(
            (
                group
                for group in groups
                if group.spec.name == CONTROLLER_CONNECTIONS
            ),
            None,
        )
        context["last_activity"] = last_activity(
            connection.instance.connection_ref
            for group in groups
            for connection in group.connections
        )
        (
            context["sight_by_provider"],
            context["unconnected_providers"],
        ) = sight_by_connection(
            {connection.instance.kind for connection in (core.connections if core else ())}
        )
        tailnet_policy, edge = observed_connection_controls(self.request.get_host())
        posture = connection_security_posture(
            groups,
            request=self.request,
            tailnet_policy=tailnet_policy,
            edge=edge,
        )
        context["connection_posture"] = posture
        context["connection_count"] = posture.connection_count
        context["unlabelled"] = [
            connection
            for connection in (core.connections if core else ())
            if connection.instance.kind == "unclassified"
        ]
        # The oldest of them, because the page's honesty depends on the staler
        # half: reporting the newest would describe a controller that is still
        # sweeping as though every row were current.
        context["observed_at"] = posture.oldest_observed_at
        return context


class TopologyView(PageMixin, LoginRequiredMixin, TemplateView):
    """The live, actionable graph derived by the application layer."""

    template_name = "control_plane/topology.html"
    page_title = "Topology"
    page_lede = (
        "Relationships between declared resources, observed systems and connections."
    )

    def get_page_actions(self):
        return (
            PageAction("Add resource", reverse("control_plane:create"), primary=True),
            PageAction("Resources", reverse("control_plane:list")),
            PageAction("Connections", reverse("control_plane:connections")),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        topology = derive_topology(
            principal=web_principal(self.request.user), request=self.request
        )
        active_lens = lens_for(self.request.GET.get("lens", "").strip())
        if active_lens is not None:
            topology = apply_lens(topology, active_lens)
        requested_focus = self.request.GET.get("focus", "").strip()
        topology, trace = apply_trace(
            topology,
            requested_focus,
            direction=self.request.GET.get("direction", "both").strip(),
            depth=self.request.GET.get("depth", "2").strip(),
        )
        by_id = {node.id: node for node in topology.nodes}
        lens_name = active_lens.name if active_lens else ""
        groups: dict[str, list[dict[str, Any]]] = {}
        detail = None
        for item in self.node_items(topology, lens_name, trace):
            groups.setdefault(item["node"].kind, []).append(item)
            if trace and item["node"].id == trace.focus:
                detail = item
        # One noun and its plural per kind, from the node kind registry: the
        # heading is the plural, and a count says one or the other.
        nouns = {kind: (item.noun, item.plural) for kind, item in NODE_KINDS.items()}
        trace_directions = (
            ("inbound", "Incoming"),
            ("outbound", "Outgoing"),
            ("both", "Both directions"),
        )
        context.update(
            {
                "topology": topology,
                "topology_groups": tuple(
                    {
                        "kind": kind,
                        "label": nouns[kind][1].capitalize(),
                        "count": counted(len(items), *nouns[kind]),
                        "items": items,
                    }
                    for kind, items in groups.items()
                ),
                # The ledger restates every edge the node bodies already state
                # from both ends, so it is drawn for a bounded trace only.
                "topology_edges": tuple(
                    {
                        "edge": edge,
                        "source": by_id[edge.source],
                        "target": by_id[edge.target],
                        "source_link": node_link(by_id[edge.source]),
                        "target_link": node_link(by_id[edge.target]),
                    }
                    for edge in topology.edges
                )
                if trace
                else (),
                # Passed rather than written into the template: the window is
                # one number, and a page that restates it drifts from the query
                # that produced the figures the moment either changes.
                "traffic_window_days": HOST_TRAFFIC_DAYS,
                "focus_node": trace.focus if trace else "",
                # The focused node's body, drawn once in the panel below the
                # lanes rather than inside its card.
                "topology_detail": detail,
                "topology_lenses": topology_lenses(),
                "active_lens": active_lens,
                "topology_trace": trace,
                "topology_trace_focus": by_id.get(trace.focus) if trace else None,
                "trace_direction_links": tuple(
                    {
                        "name": name,
                        "label": label,
                        "url": self._trace_url(
                            trace.focus,
                            name,
                            active_lens.name if active_lens else "",
                            trace.depth,
                        ),
                    }
                    for name, label in trace_directions
                )
                if trace
                else (),
                "trace_depth_links": tuple(
                    {
                        "depth": depth,
                        "url": self._trace_url(
                            trace.focus,
                            trace.direction,
                            active_lens.name if active_lens else "",
                            depth,
                        ),
                    }
                    for depth in range(1, 6)
                )
                if trace
                else (),
                "trace_reset_url": (
                    f"{reverse('control_plane:topology')}?"
                    f"{urlencode({'lens': active_lens.name})}#map"
                    if active_lens
                    else f"{reverse('control_plane:topology')}#map"
                ),
            }
        )
        return context

    @classmethod
    def node_items(
        cls,
        topology,
        lens_name: str = "",
        trace=None,
        *,
        only: str = "",
    ) -> list[dict[str, Any]]:
        """What the page states about each node, in projection order.

        ``only`` narrows to one node: its relations still come from every edge,
        so a node body fetched on its own says exactly what the page would.
        """

        by_id = {node.id: node for node in topology.nodes}
        hops = dict(trace.hops) if trace else {}
        neighbors: dict[str, set[str]] = {node.id: set() for node in topology.nodes}
        # An edge is a verb with a direction. Collapsing it to an undirected
        # neighbour set answers "is this related" and throws away "how" and
        # "which way", which is the only part an operator is actually reading.
        # Both ends get a row so a node can state its relationships from where
        # it stands, without the reader re-deriving the arrow.
        relations: dict[str, list[dict[str, Any]]] = {
            node.id: [] for node in topology.nodes
        }
        for edge in topology.edges:
            if edge.source not in neighbors or edge.target not in neighbors:
                continue
            neighbors[edge.source].add(edge.target)
            neighbors[edge.target].add(edge.source)
            evidence = {
                "detail": "" if edge.entities else edge.detail,
                "entities": edge.entities,
                "observed_age": cls._observed_age(edge.observed_at),
                "observed_at": edge.observed_at,
            }
            relation = RELATIONS.get(edge.kind)
            relations[edge.source].append(
                {
                    "direction": "out",
                    "rank": relation_rank(edge),
                    "label": edge.label,
                    "other": by_id[edge.target],
                    "other_link": node_link(by_id[edge.target]),
                    "status": edge.status,
                    "url": cls._focus_link(edge.target, lens_name),
                    **evidence,
                }
            )
            relations[edge.target].append(
                {
                    "direction": "in",
                    "rank": relation_rank(edge),
                    # Said from where this node stands: a machine "Serves" a
                    # service that "Runs on" it.
                    "label": relation.inverse if relation and relation.phrase else edge.label,
                    "other": by_id[edge.source],
                    "other_link": node_link(by_id[edge.source]),
                    "status": edge.status,
                    "url": cls._focus_link(edge.source, lens_name),
                    **evidence,
                }
            )
        items = []
        for node in topology.nodes:
            if only and node.id != only:
                continue
            rows = sorted(
                relations[node.id],
                key=lambda row: (
                    row["direction"],
                    row["rank"],
                    row["label"],
                    row["other"].label.casefold(),
                ),
            )
            items.append(
                {
                    "node": node,
                    "link": node_link(node),
                    "neighbors": " ".join(sorted(neighbors[node.id])),
                    "degree": len(neighbors[node.id]),
                    "relations": rows,
                    "observed_age": cls._observed_age(node.observed_at),
                    "observable": observable(node),
                    "hop": hops.get(node.id),
                    "focus_url": cls._focus_link(node.id, lens_name),
                    "body_url": (
                        f"{reverse('control_plane:topology_node')}?"
                        + urlencode(
                            {"node": node.id, **({"lens": lens_name} if lens_name else {})}
                        )
                    ),
                    "inbound_url": cls._trace_url(node.id, "inbound", lens_name),
                    "outbound_url": cls._trace_url(node.id, "outbound", lens_name),
                }
            )
        return items

    @staticmethod
    def _observed_age(observed_at: str) -> datetime | None:
        """The observation instant as a datetime, so a template can age it.

        A node carries the instant as ISO 8601 text because the projection is
        serialized to JSON as often as it is rendered, and `timesince` needs
        the object back. Unparseable text ages to nothing rather than raising:
        the reading is a fact about the world, not an invariant of ours.
        """

        if not observed_at:
            return None
        with suppress(ValueError):
            return datetime.fromisoformat(observed_at)
        return None

    @staticmethod
    def _focus_link(node_id: str, lens: str = "") -> str:
        """Focus one node, keeping the active lens and letting depth default."""

        return topology_url(node_id, lens=lens)

    @staticmethod
    def _trace_url(focus: str, direction: str, lens: str = "", depth: int = 3) -> str:
        return topology_url(focus, direction=direction, depth=depth, lens=lens)


class TopologyNodeView(LoginRequiredMixin, TemplateView):
    """One node's body, for the page to fetch when the node is opened.

    The page draws every node's summary and leaves the bodies to this, so
    its weight follows how many nodes there are rather than how much each
    one says.
    """

    template_name = "control_plane/_topology_node_body.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        topology = derive_topology(
            principal=web_principal(self.request.user), request=self.request
        )
        active_lens = lens_for(self.request.GET.get("lens", "").strip())
        if active_lens is not None:
            topology = apply_lens(topology, active_lens)
        items = TopologyView.node_items(
            topology,
            active_lens.name if active_lens else "",
            only=self.request.GET.get("node", "").strip(),
        )
        if not items:
            raise Http404("No such node.")
        context.update(item=items[0], traffic_window_days=HOST_TRAFFIC_DAYS)
        return context


class FindingsView(PageMixin, LoginRequiredMixin, TemplateView):
    """Evidence and safe existing actions for claims from the live topology."""

    template_name = "control_plane/findings.html"
    page_title = "Findings"

    def get_page_actions(self):
        return (
            PageAction("Action items", reverse("action_items")),
            PageAction("Topology", reverse("control_plane:topology")),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        principal = web_principal(self.request.user)
        topology = derive_topology(principal=principal)
        requested_rule = self.request.GET.get("rule", "").strip()
        active_rule = rule_for(requested_rule)
        raised = derive_findings(
            topology,
            principal=principal,
            rule=active_rule.name if active_rule else "",
        )
        entries = []
        for finding in raised:
            entries.append(
                {
                    "finding": finding,
                    "workflow": finding.workflow,
                    "investigations": finding.investigations,
                    "offers": finding.offers,
                    "remedies": tuple(
                        remedy for remedy in finding.remedies if remedy.url
                    ),
                }
            )

        counts: dict[str, int] = {}
        for finding in raised:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        context.update(
            {
                "finding_entries": tuple(entries),
                # Only rules that raised something, or the one being viewed. A
                # chip for every rule HQ knows is a list of what is fine.
                "finding_rules": tuple(
                    rule
                    for rule in finding_rules()
                    if rule.name in {finding.rule for finding in raised}
                    or (active_rule and rule.name == active_rule.name)
                ),
                "active_rule": active_rule,
                "finding_counts": counts,
            }
        )
        return context


class ApprovalListView(LoginRequiredMixin, View):
    """Retired: redirects to the audit log's awaiting view."""

    def get(self, request):
        return redirect(f"{reverse('core:audit_list')}?awaiting=1")


class ApprovalDecisionView(LoginRequiredMixin, View):
    """Agree to a held change, or refuse it. Nothing else can.

    A POST from a signed-in operator, which is the entire mechanism: the
    interface is the check, and ``application.approvals`` makes it rather than
    this view, so a second surface cannot forget to.
    """

    def post(self, request, approval_id, decision=None):
        from application import approvals

        decision = decision or request.POST.get("decision", "")
        try:
            if decision == "approve":
                approvals.approve(str(approval_id), principal=web_principal(request.user))
                messages.success(request, "Approved and applied.")
            elif decision == "reject":
                approvals.reject(
                    str(approval_id),
                    principal=web_principal(request.user),
                    note=request.POST.get("note", ""),
                )
                messages.success(request, "Rejected. Nothing was applied.")
            else:
                messages.error(request, "Choose whether to approve or reject.")
        except (AuthorizationError, ValueError) as exc:
            # One handler for both, because a person reading this page is owed
            # the same treatment either way: an approval that cannot be applied
            # says why, on the page, with the request left as it was.
            messages.error(request, str(exc) or "Could not record that decision.")
        return redirect(
            safe_next(request, fallback=f"{reverse('core:audit_list')}?awaiting=1")
        )


class InfrastructureListView(PageMixin, LoginRequiredMixin, ListView):
    model = ManagedResource
    template_name = "control_plane/resource_list.html"
    context_object_name = "resources"
    page_title = "Infrastructure"
    page_lede = "Declared resources and their last observed state."

    def get_page_actions(self):
        return (
            PageAction("Add", reverse("control_plane:create"), primary=True),
            PageAction("Services", reverse("control_plane:services")),
            PageAction("Provider schemas", reverse("control_plane:providers")),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Read once for the whole page. Every row that forwards somewhere asks
        # the same question of the same few tables, and asked per row it is a
        # query budget that grows with the estate.
        machines = declared_machines()
        targets = delivery_targets()
        at = whereabouts(machines)
        for resource in context["resources"]:
            resource.control_health = resource_health(resource)
            # Where it sends traffic, named rather than addressed, matching
            # what the resource's own page has always said.
            resource.origin_machine = _origin_machine(resource, machines, at, targets)
            # What it is, in the provider's own words.
            rows = _readout_rows(resource)
            resource.summary = rows[0][1] or rows[0][2] if rows else ""
            resource.converges = kind_converges(resource.kind)
        context["operations"] = OperationRequest.objects.select_related("resource")[:12]
        context["provider_catalog"] = describe_providers()
        return context


class InfrastructureDetailView(PageMixin, LoginRequiredMixin, DetailView):
    model = ManagedResource
    slug_field = "key"
    slug_url_kwarg = "key"
    template_name = "control_plane/resource_detail.html"
    context_object_name = "resource"

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        home = self.object.get_absolute_url()
        if home != request.path:
            # A kind with a page of its own, such as a machine, lives there.
            return redirect(home)
        return super().get(request, *args, **kwargs)

    @cached_property
    def capabilities(self):
        return resource_capabilities(self.object)

    @cached_property
    def container(self):
        """What the sweep knows about a watched container, and nothing otherwise.

        A container declares identity and nothing else, so everything worth
        opening the page for is a join: which machine that name is, what the
        container is doing, and which services reach it through the ports it
        publishes. All three come from the sweep this resource was adopted out
        of, so none of it is a second opinion about anything.
        """

        if self.object.kind != CONTAINER_KIND:
            return None
        return container_context(
            self.object.spec.get("host", ""), self.object.spec.get("name", "")
        )

    def get_page_title(self):
        return self.object.key

    def get_page_lede(self):
        return kind_label(self.object.kind)

    @cached_property
    def relationships(self):
        return relationships_for(
            f"resource:{self.object.key}", principal=web_principal(self.request.user)
        )

    @cached_property
    def home(self):
        """The machine, service or domain this declaration belongs to, if any."""

        return next(
            (
                item.entity
                for group in self.relationships.groups
                for item in group.items
                if item.entity.kind in ("machine", "service", "zone")
            ),
            None,
        )

    def get_page_trail(self):
        crumbs = []
        if self.home and self.home.url:
            crumbs.append((self.home.label, self.home.url))
        return tuple(crumbs)

    def get_page_actions(self):
        if self.capabilities.removal_pending:
            return ()
        key = self.object.key
        capabilities = self.capabilities
        actions = [
            PageAction(
                VERB_LABELS[verb],
                reverse(f"control_plane:{verb}", args=[key]),
                method="post",
                primary=verb == "renew",
                disabled=not allowed.enabled,
                title="" if allowed.enabled else allowed.reason,
            )
            for verb, allowed in capabilities.page_actions
        ]
        actions.append(
            PageAction(
                "Edit",
                returning_to(
                    reverse("control_plane:edit", args=[key]),
                    self.request.get_full_path(),
                ),
            )
        )
        if PROVIDERS[self.object.kind].material_form:
            actions.append(
                PageAction(
                    "Replace certificate"
                    if getattr(self.object, "material", None)
                    else "Upload certificate",
                    reverse("control_plane:upload_certificate", args=[key]),
                    primary=True,
                )
            )
        if capabilities.removal != "unavailable":
            actions.append(
                PageAction(
                    "Stop managing" if capabilities.removal == "forget" else "Remove",
                    reverse("control_plane:remove", args=[key]),
                    danger=True,
                )
            )
        return tuple(actions)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        capabilities = self.capabilities
        context["capabilities"] = capabilities
        context["controller_rows"] = tuple(
            (VERB_LABELS.get(verb, verb.replace("-", " ").capitalize()), allowed)
            for verb, allowed in capabilities.actions.items()
        )
        context["controller_automatic"] = any(
            allowed.automatic for allowed in capabilities.actions.values()
        )
        context["control_health"] = resource_health(self.object)
        context["sync_state"] = (
            "in_sync"
            if self.object.generation == self.object.observed_generation
            else "pending"
        )
        # What this resource does, said by its own provider.
        context["label"] = kind_label(self.object.kind)
        context["service_links"] = _service_links(self.object)
        # Where this resource sends traffic, when it sends it anywhere. A
        # provider that declares an origin is one whose thing runs on a machine,
        # so the machine is a link rather than an address printed in a readout.
        context["origin_machine"] = _origin_machine(self.object)
        context["provider_machine"] = _provider_machine(self.object)
        if self.container is not None:
            context["container"] = self.container
        context["removal_pending"] = self.capabilities.removal_pending
        # Nothing for a container: the panel above is the sweep's answer and
        # the readout is the declaration's, and a container declares identity
        # and nothing else. So it could only repeat what the panel had just said
        # better -- "State: running, up 3 months" followed by "State: --", and
        # the container's own name under a page titled after it.
        # A change to this resource that a credential asked for and nobody has
        # answered. Said on the resource's own page as well as on the queue,
        # because this is the page an operator opens when they wonder why a
        # declaration has not moved, and "something is waiting for you" is the
        # answer, rather than a resource that merely looks idle.
        from application.approvals import pending as pending_approvals

        context["awaiting_approval"] = tuple(
            held for held in pending_approvals() if held.resource_key == self.object.key
        )
        context["readout_rows"] = (
            ()
            if self.object.kind == CONTAINER_KIND
            else _linked_readout(self.object, self.relationships)
        )
        context["relationships"] = self.relationships
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
            operation_summary(operation)
            for operation in self.object.operations.all()[:20]
        ]
        for operation in context["operations"]:
            operation["created_at"] = datetime.fromisoformat(operation["created_at"])
            if operation["completed_at"]:
                operation["completed_at"] = datetime.fromisoformat(
                    operation["completed_at"]
                )
        context["resolved_spec"] = None
        if self.object.kind == CERTIFICATE_KIND:
            context["resolved_spec"] = self.object.spec
            try:
                context["resolved_spec"] = controller_contract(self.object)["resource"][
                    "spec"
                ]
                context["resolution_error"] = ""
                observed_names: dict[str, set[str]] = {}
                for observation in self.object.status.get("consumers", []):
                    observed_names.setdefault(
                        observation.get("consumer", ""), set()
                    ).add(observation.get("domain", ""))
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


# What each controller verb is called in a sentence. One entry per verb, so a
# new verb needs no new view class.
OPERATION_PHRASE = {
    OperationRequest.Action.RECONCILE: "reconciliation",
    OperationRequest.Action.RENEW: "certificate renewal",
    OperationRequest.Action.DELETE: "removal",
    OperationRequest.Action.RESTART: "a restart",
    OperationRequest.Action.START: "a start",
    OperationRequest.Action.STOP: "a stop",
    OperationRequest.Action.APPROVE_ROUTES: "route approval",
}


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


class OperationView(LoginRequiredMixin, View):
    """Ask the controller for one action on one resource.

    The action comes from the URL rather than from the class, so adding a verb
    is a route and a phrase rather than another view that does what this one
    already does.
    """

    action = OperationRequest.Action.RECONCILE

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        # Back where the verb was offered. These forms are on pages that show
        # the fact the verb answers (a machine's routes, a service's
        # container) and they have been sending `next` all along while this
        # view returned to the resource record regardless. Validated through
        # the shared helper, so the field cannot become an open redirect.
        destination = safe_next(
            request, fallback=reverse("control_plane:detail", kwargs={"key": key})
        )
        try:
            result = _web_operation(request, resource, self.action)
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect(destination)
        verb = "Queued" if result["queued"] else "Already queued"
        phrase = OPERATION_PHRASE.get(self.action, self.action)
        messages.success(request, f"{verb} {phrase} for “{resource.key}”.")
        return redirect(destination)


class CertificateDownloadView(LoginRequiredMixin, View):
    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        certificate_pem = resource.status.get("certificate_pem", "")
        if resource.kind != CERTIFICATE_KIND or not certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "No verified certificate to download."},
                status=404,
            )
        if "PRIVATE KEY-----" in certificate_pem:
            return JsonResponse(
                {"ok": False, "error": "Refused: the certificate file contains a private key."},
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
    and it says out loud which two things swapped, which a drag does not.
    """

    def post(self, request, hostname: str):
        from application.pins import SERVICE, move

        name = normalized_hostname(hostname)
        if not name:
            raise Http404("No such service.")
        delta = -1 if request.POST.get("direction") == "up" else 1
        move(request.user, SERVICE, name, delta)
        return redirect(safe_next(request) or reverse("control_plane:services"))


class ToolsView(PageMixin, LoginRequiredMixin, TemplateView):
    """The tools, one tab at a time, answering on a plain GET.

    Deliberately thin. Every tool here is backed by registered capabilities,
    so the generic pages at `/commands/<name>/` already render a form,
    authorize it, execute it and print the result. This page exists because
    those are one tool each, and the question an operator actually has is
    usually several at once.

    So it holds no tool logic: it picks a tab from `application.toolkit`, runs
    that tab's capabilities through the same authorization every other adapter
    uses, and hands the results to the tab's own partial.

    A lookup someone typed is a GET, so a result is a URL. Re-reading a stored
    answer replaces it, so that is a POST, which redirects back to the result.
    """

    template_name = "control_plane/tools.html"
    page_title = "Tools"
    page_lede = "Lookups run from outside this network."

    def get_context_data(self, **kwargs):
        from application.capabilities import execute_capability
        from application.security import web_principal
        from application.toolkit import tab_named, tabs_for

        context = super().get_context_data(**kwargs)
        principal = web_principal(self.request.user)
        tabs = tabs_for(principal)
        current = tab_named(self.request.GET.get("tab", ""), principal)
        context["tabs"] = tabs
        context["tab"] = current
        if current is None:
            return context

        # Only what this tab offers, and only what was actually asked. An empty
        # field is not a lookup of the empty string.
        asked = {
            name: self.request.GET.get(name.rpartition(".")[2], "").strip()
            for name in current.capabilities
        }
        context["asked"] = asked
        context["results"] = {
            name: execute_capability(
                name, {name.rpartition(".")[2]: value}, principal=principal
            )
            for name, value in asked.items()
            if value
        }
        # The capability answers with an ISO string, which is what the session
        # and the machine API need. A template wants a datetime, so that HQ's
        # own DATETIME_FORMAT applies rather than a second date style appearing
        # on one page.
        for reading in context["results"].values():
            stamp = reading.get("observed_at") if isinstance(reading, dict) else None
            if stamp:
                with suppress(ValueError):
                    reading["observed_at"] = datetime.fromisoformat(stamp)
        return context

    def post(self, request, *args, **kwargs):
        """Re-read an address's stored answer, then show it."""

        from urllib.parse import urlencode

        from application.capabilities import execute_capability
        from application.security import web_principal

        address = request.POST.get("address", "").strip()
        if address:
            execute_capability(
                "lookup.address",
                {"address": address, "refresh": True},
                principal=web_principal(request.user),
            )
        query = urlencode({"tab": request.POST.get("tab", ""), "address": address})
        return redirect(f"{reverse('control_plane:tools')}?{query}")


