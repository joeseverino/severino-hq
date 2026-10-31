import json
from functools import cached_property
from typing import Any, override

from django.contrib import messages
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView, ListView

from hq.platform.application.action_links import READ_NOW_CAPABILITY, command_url
from hq.platform.application.approvals import DESTRUCTIVE_EFFECT
from hq.platform.application.capabilities import capability_registry, capability_words, execute_capability
from hq.platform.application.entity_links import kind_label, record_name, web_url
from hq.platform.application.infrastructure import (
    declared_machines,
    delivery_targets,
    is_drifted,
    serialize_public_status,
    serialize_resource,
)
from hq.platform.application.integration_specs import requested as requested_note
from hq.platform.application.inventory import service_hostnames
from hq.platform.application.labels import field_label
from hq.platform.application.pages import PageAction, PageMixin, page_context
from hq.platform.application.payload_words import value_words
from hq.platform.application.relationships import relationships_for
from hq.platform.application.resource_capabilities import (
    Allowed,
    channels_inside,
    resource_capabilities,
    verb_words,
)
from hq.platform.application.resource_context import (
    controller_summary,
    newest_reading,
    origin_machine,
    record_list,
    record_status,
    record_summary,
    resource_context,
)
from hq.platform.application.resource_operations import (
    ACTION_CAPABILITY,
    HISTORY_WINDOW,
    changes,
    operation_summary,
    requested_by,
    resource_history,
    supervision,
)
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.timestamps import moment
from hq.platform.application.topology_model import RELATIONS
from hq.platform.application.ui import counted
from hq.platform.application.whereabouts import whereabouts
from hq.platform.core.templatetags.nav_tags import returning_to
lazy from hq.platform.application.page_relations import for_container

from .models import ManagedResource, OperationRequest
from .provider_adapters.portainer import CONTAINER_KIND
from .provider_adapters.tls import CERTIFICATE_KIND
from .providers import PROVIDERS, describe_providers, readout_rows, shown_spec
lazy from .container_views import container_detail


def _web_operation(request, resource, action) -> dict[str, Any]:
    """Ask for ``action`` on ``resource`` through its capability, as the operator."""

    return execute_capability(
        ACTION_CAPABILITY[action],
        {"reason": request.POST.get("reason", "").strip()},
        principal=web_principal(request.user),
        target=resource.key,
    )


def _json(value: Any) -> str:
    """A stored document as JSON a person can read and paste, or "" for none."""

    return json.dumps(value, indent=2, sort_keys=True, default=str) if value else ""


def _spec_rows(resource, *, beside_readout: bool = False) -> dict[str, tuple[tuple[str, str], ...]]:
    """A spec as an operator reads it before a destructive action, split the way
    the form splits it: fields by their model titles, an unset optional left out
    rather than shown as "None", and fields the provider declares routine folded.

    ``beside_readout`` is the record's own page, where the readout and the page's
    name stand above this: a value they already show is not repeated.
    """

    provider = PROVIDERS[resource.kind]
    fields = provider.spec_type.model_fields
    # What the readout above already printed. On anything with a handful of
    # fields the readout *is* the spec, and the disclosure would repeat it
    # whole.
    rows = readout_rows(resource)
    shown = {str(label).strip().casefold() for label, _, _ in rows}
    # A value the readout or the page's own name already shows is not said
    # again under a second label.
    said: set[str] = set()
    if beside_readout:
        said = {str(value) for _, desired, observed in rows for value in (desired, observed) if value}
        said.update(service_hostnames(resource.kind, resource.spec))
    primary: list[tuple[str, str]] = []
    advanced: list[tuple[str, str]] = []
    for name, value in shown_spec(resource.kind, resource.spec).items():
        # A reported field's setting is the fixed goal its readout already states.
        if value is None or name in provider.reported_fields:
            continue
        label = field_label(name, fields[name].title) if name in fields else name
        if label.strip().casefold() in shown:
            continue
        rendered = value_words(value)
        # And not the thing the page is already titled. A machine's name is its
        # identifier here, so the last row left after the readout would be the
        # heading repeated inside a disclosure offering more.
        #
        # Only when they are the same string: a declaration whose name differs
        # from its filing is telling you something, and that is the case worth
        # showing.
        if rendered == resource.key or (rendered in said and not isinstance(value, bool)):
            continue
        row = (label, rendered)
        (advanced if name in provider.advanced_fields else primary).append(row)
    return {"primary": tuple(primary), "advanced": tuple(advanced)}


def _linked_readout(resource, relationships) -> tuple[tuple[str, str, str, tuple], ...]:
    """The readout, with each value that names a related entity as its link.

    A blank connection field is filled from the connections the relation graph
    says use this declaration.
    """

    related = {item.entity.label: item.entity for group in relationships.groups for item in group.items}
    connections = tuple(
        item.entity for group in relationships.groups for item in group.items if item.entity.kind == "connection"
    )
    field = PROVIDERS[resource.kind].spec_type.model_fields.get("connection_ref")
    connection_label = (field.title or "") if field is not None else ""
    rows = []
    for label, desired, observed in readout_rows(resource):
        value = str(observed or desired or "")
        if value in related:
            links = (related[value],)
        elif not value and connection_label and label == connection_label:
            links = connections
        else:
            links = ()
        rows.append((label, desired, observed, links))
    return tuple(rows)


def _removal_note(resource) -> str:
    """What this particular removal costs, if the provider says."""

    note = PROVIDERS[resource.kind].removal_note
    if note is None:
        return ""
    try:
        return note(resource.spec)
    except KeyError, TypeError, ValueError:
        # A confirmation page that cannot render is worse than one missing a
        # sentence, and this is the page an operator uses to stop.
        return ""


class ResourceRemoveView(View):
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
                    "label": capabilities.removal_words,
                    "cancel_url": resource.get_absolute_url(),
                },
                **page_context(
                    f"{capabilities.removal_words}: {record_name(resource.kind, resource.spec, resource.key)}?",
                    kind_label(resource.kind),
                ),
            },
        )

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        result = _web_operation(request, resource, OperationRequest.Action.DELETE)
        if not result["ok"]:
            messages.error(request, result["error"]["message"])
            return redirect("control_plane:detail", key=key)
        if "forgotten" in result:
            # Nothing was queued because nothing exists at the provider that HQ
            # made. Saying "queued removal" here would promise a deletion that
            # is neither happening nor wanted.
            released = len(result["released"])
            messages.success(
                request,
                f"Stopped tracking “{result['forgotten']}”"
                + (f" and {counted(released, 'record')} in it" if released else "")
                + ". Nothing live was changed.",
            )
            return redirect("control_plane:list")
        messages.success(request, requested(OperationRequest.Action.DELETE, resource.key, queued=result["queued"]))
        return redirect("control_plane:detail", key=key)


class InfrastructureListView(PageMixin, ListView):
    model = ManagedResource
    template_name = "control_plane/resource_list.html"
    context_object_name = "resources"
    page_title = "All records"

    @override
    def get_page_actions(self):
        return (
            PageAction(
                capability_words("infrastructure.resource.create"), reverse("control_plane:create"), primary=True
            ),
            PageAction("Services", reverse("control_plane:services")),
            PageAction("Setting reference", reverse("control_plane:providers")),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Read once for the whole page. Every row that forwards somewhere asks
        # the same question of the same few tables, and asked per row it is a
        # query budget that grows with the estate.
        machines = declared_machines()
        targets = delivery_targets()
        at = whereabouts(machines)
        newest: dict[str, Any] = {}
        for resource in context["resources"]:
            read_at = resource.last_observed_at
            if read_at and (resource.kind not in newest or read_at > newest[resource.kind]):
                newest[resource.kind] = read_at
        for resource in context["resources"]:
            resource.record_status = record_status(resource, newest=newest.get(resource.kind))
            # Where it sends traffic, named rather than addressed, matching
            # what the resource's own page has always said.
            resource.origin_machine = origin_machine(resource, machines, at, targets)
            # The name the row's link shows, and the line under it.
            resource.shown_name = record_name(resource.kind, resource.spec, resource.key)
            resource.summary = record_summary(resource, resource.shown_name)
        context["changes"] = [
            {
                "resource": operation.resource.key,
                "action": operation.get_action_display(),
                "summary": operation_summary(operation),
                "by": requested_by(operation),
                "at": operation.created_at,
            }
            for operation in changes(OperationRequest.objects.select_related("resource")[:HISTORY_WINDOW], 12)
        ]
        context["provider_catalog"] = describe_providers()
        context["records"] = record_list(
            context["resources"],
            query=self.request.GET.get("q", ""),
            kind=self.request.GET.get("type", ""),
        )
        return context


class InfrastructureDetailView(PageMixin, DetailView):
    model = ManagedResource
    slug_field = "key"
    slug_url_kwarg = "key"
    template_name = "control_plane/resource_detail.html"
    context_object_name = "resource"

    @cached_property
    def _record(self):
        return super().get_object()

    @override
    def get_object(self, queryset=None):
        # Read once: ``get`` asks before deciding where the record lives, and
        # the detail view asks again to draw it.
        return self._record

    @override
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        home = self.object.get_absolute_url()
        if home != request.path:
            # A kind with a page of its own, such as a machine, lives there.
            return redirect(home)
        return super().get(request, *args, **kwargs)

    @cached_property
    def derived(self):
        return resource_context(self.object)

    @cached_property
    def capabilities(self):
        return self.derived.capabilities

    @cached_property
    def container(self):
        """What the sweep knows about a watched container, and nothing otherwise."""

        if self.object.kind != CONTAINER_KIND:
            return None

        return container_detail(self.object, self.request)

    @override
    def get_page_title(self):
        # A container by its own name: its machine is the trail above it.
        if self.object.kind == CONTAINER_KIND and self.object.spec.get("name"):
            return self.object.spec["name"]
        return record_name(self.object.kind, self.object.spec, self.object.key)

    @override
    def get_page_lede(self):
        # A record whose name is its type says what it does instead.
        label = kind_label(self.object.kind)
        return PROVIDERS[self.object.kind].summary if label == self.get_page_title() else label

    @cached_property
    def all_relationships(self):
        """Every relation, for what the page derives from them (its home)."""

        return relationships_for(f"resource:{self.object.key}", principal=web_principal(self.request.user))

    @cached_property
    def relationships(self):
        """The relations the section shows: less what the page says elsewhere."""

        found = self.all_relationships
        if self.object.kind == CONTAINER_KIND:
            return for_container(found)
        return found

    @cached_property
    def home(self):
        """The machine, service or domain this declaration belongs to, if any."""

        return next(
            (
                item.entity
                for group in self.all_relationships.groups
                for item in group.items
                if item.entity.kind in ("machine", "service", "zone")
            ),
            None,
        )

    @override
    def get_page_trail(self):
        crumbs = []
        if self.home and self.home.url:
            crumbs.append((self.home.label, self.home.url))
        return tuple(crumbs)

    @override
    def get_page_actions(self):
        if self.capabilities.removal_pending:
            return ()
        key = self.object.key
        capabilities = self.capabilities
        drifted = is_drifted(self.object)
        provider = PROVIDERS[self.object.kind]
        actions = [
            verb_action(
                verb,
                key,
                allowed,
                # Applying a record that only reports on itself reads it again.
                label=(
                    capability_words(READ_NOW_CAPABILITY)
                    if verb == "reconcile" and provider.reported_fields
                    else verb_words(verb)
                ),
                title="" if allowed.enabled else allowed.reason,
                origin=self.request.get_full_path(),
                primary=verb == "renew",
            )
            for verb, allowed in capabilities.page_actions
        ]
        if drifted:
            # Changed outside HQ. Keeping that loses nothing, so it leads, and
            # restoring HQ's copy is the button beside it rather than the only one.
            actions.insert(
                0,
                PageAction(
                    capability_words("infrastructure.resource.accept_observed"),
                    returning_to(
                        command_url("infrastructure.resource.accept_observed", key),
                        self.request.get_full_path(),
                    ),
                    primary=True,
                ),
            )
        console = (
            web_url(provider.console({**self.object.spec, **(self.object.status or {})}))
            if provider.console and provider.console_label
            else ""
        )
        if console:
            actions.insert(0, PageAction(provider.console_label, console, primary=not drifted))
        actions.append(
            PageAction(
                capability_words("infrastructure.resource.update"),
                returning_to(
                    reverse("control_plane:edit", args=[key]),
                    self.request.get_full_path(),
                ),
            )
        )
        if PROVIDERS[self.object.kind].material_form:
            actions.append(
                PageAction(
                    "Replace certificate" if getattr(self.object, "material", None) else "Upload certificate",
                    reverse("control_plane:upload_certificate", args=[key]),
                    primary=True,
                )
            )
        if capabilities.removal != "unavailable":
            actions.append(
                PageAction(
                    capabilities.removal_words,
                    reverse("control_plane:remove", args=[key]),
                    danger=True,
                )
            )
        return tuple(actions)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        capabilities = self.capabilities
        context["capabilities"] = capabilities
        context["controller"] = controller_summary(capabilities.actions, verb_words)
        derived = self.derived
        context["status"] = record_status(self.object, health=derived.health, newest=newest_reading(self.object.kind))
        # A running container is summarized by what it runs, in its own band.
        if not (self.container and self.container["standing"]):
            context["page_aside"] = "control_plane/_record_status.html"
        # What this resource does, said by its own provider.
        context["label"] = kind_label(self.object.kind)
        # Relationships names the service it is for; the head says it only when
        # that section does not.
        relations = {group.phrase for group in self.relationships.groups}
        context["service_links"] = () if RELATIONS["declared_by"].inverse in relations else derived.service_links
        # Where this resource sends traffic, when it sends it anywhere, and the
        # machine running the provider that manages it.
        context["origin_machine"] = derived.origin_machine
        # A DNS record answers with an address; a proxy sends requests on.
        context["origin_phrase"] = "Points to" if PROVIDERS[self.object.kind].answers else "Forwards to"
        context["provider_machine"] = derived.provider_machine
        context["managing_connections"] = tuple(
            item.entity
            for group in self.relationships.groups
            for item in group.items
            if item.entity.kind == "connection"
        )
        if self.container is not None:
            context["container"] = self.container
        context["removal_pending"] = capabilities.removal_pending
        # A change a credential asked for that nobody has answered, said on the
        # page an operator opens when a declaration has not moved.
        context["awaiting_approval"] = derived.awaiting_approval
        # Nothing for a container: its panel is the sweep's answer and a
        # container declares identity and nothing else.
        context["readout_rows"] = (
            () if self.object.kind == CONTAINER_KIND else _linked_readout(self.object, self.relationships)
        )
        context["relationships"] = self.relationships
        context["spec_rows"] = _spec_rows(self.object, beside_readout=True)
        context["renewal_at"] = derived.expiry.renewal_at if derived.expiry else None
        context["operations"] = resource_history(self.object)
        for operation in context["operations"]:
            operation["created_at"] = moment(operation["created_at"], naive="keep")
            operation["completed_at"] = moment(operation["completed_at"], naive="keep")
            operation["raw_result"] = _json(operation["raw_result"])
        context["resolved_spec"] = derived.resolved_spec
        context["resolution_error"] = derived.resolution_error
        context["display_consumers"] = derived.display_consumers
        context["certificate_use"] = derived.certificate_use
        context["spec_json"] = _json(self.object.spec)
        context["diagnostic_status"] = _json(serialize_public_status(self.object.status))
        return context


def requested(action: str, key: str, *, queued: bool) -> str:
    """What a press of one of a resource's verbs says, in the verb's own words."""

    return requested_note(verb_words(action), key, again=not queued)


def is_destructive(verb: str) -> bool:
    """Whether a resource's verb takes something away from whoever is using it,
    as its capability declares: asked for by name on a page of its own, never
    pressed straight from a head."""

    return capability_registry()[ACTION_CAPABILITY[verb]].effect == DESTRUCTIVE_EFFECT


def verb_action(
    verb: str, key: str, allowed: Allowed, *, label: str, title: str, origin: str, primary: bool = False
) -> PageAction:
    """One of a resource's verbs as a head control. A destructive one opens its
    confirmation, named, and comes back to ``origin``; the rest post at once."""

    url = reverse(f"control_plane:{verb}", args=[key])
    if is_destructive(verb):
        return PageAction(label, returning_to(url, origin), danger=True, disabled=not allowed.enabled, title=title)
    return PageAction(label, url, method="post", primary=primary, disabled=not allowed.enabled, title=title)


class OperationView(View):
    """Ask the controller for one action on one resource.

    The action comes from the URL rather than from the class, so adding a verb
    is a route and a phrase rather than another view that does what this one
    already does.
    """

    action = OperationRequest.Action.RECONCILE

    def get(self, request, key):
        """A destructive verb's confirmation, naming what stops; any other verb only posts."""

        if not is_destructive(self.action):
            return HttpResponseNotAllowed(["POST"])
        resource = get_object_or_404(ManagedResource, key=key)
        name = record_name(resource.kind, resource.spec, resource.key)
        label = f"{verb_words(self.action)} {name}"
        return render(
            request,
            "control_plane/resource_confirm_verb.html",
            {
                "name": name,
                # What else stops with it: HQ's own way of reaching a machine,
                # said before the press, since HQ cannot undo it from here.
                "host": resource.spec.get("host", ""),
                "channels": channels_inside(resource),
                # The unit that runs it, which the verb goes through.
                "supervision": supervision(resource),
                "confirm": {
                    "url": request.get_full_path(),
                    "label": label,
                    "cancel_url": safe_next(request, fallback=resource.get_absolute_url()),
                },
                **page_context(f"{label}?", kind_label(resource.kind)),
            },
        )

    def post(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        # Back where the verb was offered. These forms are on pages that show
        # the fact the verb answers (a machine's routes, a service's
        # container) and send `next`. Validated through the shared helper, so
        # the field cannot become an open redirect.
        destination = safe_next(request, fallback=reverse("control_plane:detail", kwargs={"key": key}))
        result = _web_operation(request, resource, self.action)
        if not result["ok"]:
            messages.error(request, result["error"]["message"])
            return redirect(destination)
        messages.success(request, requested(self.action, resource.key, queued=result["queued"]))
        return redirect(destination)


class CertificateDownloadView(View):
    def get(self, _request, key):
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
        response["Content-Disposition"] = f'attachment; filename="{resource.key}-public.pem"'
        return response


class ResourceReportDownloadView(View):
    def get(self, _request, key):
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
        response["Content-Disposition"] = f'attachment; filename="{resource.key}-status.json"'
        return response


class ProviderSchemaView(View):
    def get(self, _request):
        return JsonResponse(describe_providers(), json_dumps_params={"indent": 2})
