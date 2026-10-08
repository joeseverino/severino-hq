import json
import uuid
from functools import cached_property
from typing import Any, override

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView, ListView

from hq.platform.application.action_links import command_url
from hq.platform.application.entity_links import kind_label, record_name, web_url
from hq.platform.application.infrastructure import (
    PolicyError,
    declared_machines,
    delivery_targets,
    is_drifted,
    serialize_public_status,
    serialize_resource,
)
from hq.platform.application.inventory import service_hostnames
from hq.platform.application.pages import PageAction, PageMixin, page_context
from hq.platform.application.relationships import relationships_for
from hq.platform.application.resource_capabilities import (
    VERB_LABELS,
    resource_capabilities,
)
from hq.platform.application.resource_context import (
    controller_summary,
    newest_reading,
    origin_machine,
    readout_rows,
    record_list,
    record_status,
    resource_context,
)
from hq.platform.application.resource_operations import (
    ACTION_LABELS,
    HISTORY_WINDOW,
    OperationCommand,
    changes,
    operation_summary,
    request_certificate_renewal,
    request_lifecycle,
    request_reconcile,
    request_removal,
    requested_by,
    resource_history,
)
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.timestamps import moment
from hq.platform.application.topology_model import RELATIONS
from hq.platform.application.ui import counted
from hq.platform.application.whereabouts import whereabouts
from hq.platform.core.templatetags.nav_tags import returning_to

from .models import ManagedResource, OperationRequest
from .provider_adapters.portainer import CONTAINER_KIND
from .provider_adapters.tls import CERTIFICATE_KIND
from .providers import PROVIDERS, describe_providers

# Which use case serves which verb. A table rather than a fall-through, so a
# lifecycle verb such as Restart never reaches reconciliation, which is locked
# for a container.
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
    return request_lifecycle(command, principal=principal, current_key=resource.key, action=action)


def _json(value: Any) -> str:
    """A stored document as JSON a person can read and paste, or "" for none."""

    return json.dumps(value, indent=2, sort_keys=True, default=str) if value else ""


def _spec_value(value: Any) -> str:
    """One spec field as a person reads it: a list as its items, not its repr."""

    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _spec_rows(resource, *, beside_readout: bool = False) -> dict[str, tuple[tuple[str, str], ...]]:
    """A spec as an operator reads it, split the way the form splits it.

    Shown before a destructive action, so:

    - Fields by their titles from the model, never their names.
    - An unset optional is left out rather than shown as "None".
    - Fields the provider declares routine fold away, the same split the form
      makes.

    ``beside_readout`` is the record's own page, where the readout and the
    page's name stand above this: a value they already show is not repeated.
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
    for name, value in resource.spec.items():
        # A reported field's setting is the fixed goal its readout already states.
        if value is None or name in provider.reported_fields:
            continue
        label = fields[name].title or name.replace("_", " ").capitalize() if name in fields else name
        if label.strip().casefold() in shown:
            continue
        rendered = _spec_value(value)
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
                    "label": "Stop tracking in HQ" if forget else "Remove",
                    "cancel_url": resource.get_absolute_url(),
                },
                **page_context(
                    f"Remove {record_name(resource.kind, resource.spec, resource.key)}?",
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
                f"Stopped tracking “{result['forgotten']}”"
                + (f" and {counted(released, 'record')} in it" if released else "")
                + ". Nothing live was changed.",
            )
            return redirect("control_plane:list")
        verb = "Queued" if result["queued"] else "Already queued"
        messages.success(
            request,
            f"{verb} removal of “{resource.key}”. HQ forgets it once it is confirmed gone.",
        )
        return redirect("control_plane:detail", key=key)


class InfrastructureListView(PageMixin, ListView):
    model = ManagedResource
    template_name = "control_plane/resource_list.html"
    context_object_name = "resources"
    page_title = "All records"

    @override
    def get_page_actions(self):
        return (
            PageAction("Add a record", reverse("control_plane:create"), primary=True),
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
            # What it is, in the provider's own words.
            rows = readout_rows(resource)
            resource.summary = rows[0][1] or rows[0][2] if rows else ""
            # The name the row's link shows, so the line under it never repeats it.
            resource.shown_name = record_name(resource.kind, resource.spec, resource.key)
        context["changes"] = [
            {
                "resource": operation.resource.key,
                "action": ACTION_LABELS.get(operation.action, operation.get_action_display()),
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

    @override
    def get_object(self, queryset=None):
        # Read once: ``get`` asks before deciding where the record lives, and
        # the detail view asks again to draw it.
        if not hasattr(self, "_record"):
            self._record = super().get_object(queryset)
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
        from .container_views import container_detail

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
            from hq.platform.application.page_relations import for_container

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
        # Applying a record that only reports on itself reads it again.
        labels = {
            **VERB_LABELS,
            **({"reconcile": "Read now"} if provider.reported_fields else {}),
            **({"reconcile": "Restore HQ's version"} if drifted else {}),
        }
        actions = [
            PageAction(
                labels[verb],
                reverse(f"control_plane:{verb}", args=[key]),
                method="post",
                primary=verb == "renew",
                disabled=not allowed.enabled,
                title="" if allowed.enabled else allowed.reason,
            )
            for verb, allowed in capabilities.page_actions
        ]
        if drifted:
            # Changed outside HQ. Keeping that loses nothing, so it leads, and
            # restoring HQ's copy is the button beside it rather than the only one.
            actions.insert(
                0,
                PageAction(
                    "Keep the live version",
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
                "Change what HQ expects",
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
                    "Stop tracking in HQ" if capabilities.removal == "forget" else "Remove",
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
        context["controller"] = controller_summary(
            capabilities.actions, lambda verb: VERB_LABELS.get(verb, verb.replace("-", " ").capitalize())
        )
        derived = self.derived
        context["status"] = record_status(self.object, health=derived.health, newest=newest_reading(self.object.kind))
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


# What each controller verb is called in a sentence. One entry per verb, so a
# new verb needs no new view class.
OPERATION_PHRASE = {
    OperationRequest.Action.RECONCILE: "applying HQ's settings",
    OperationRequest.Action.RENEW: "certificate renewal",
    OperationRequest.Action.DELETE: "removal",
    OperationRequest.Action.RESTART: "a restart",
    OperationRequest.Action.START: "a start",
    OperationRequest.Action.STOP: "a stop",
    OperationRequest.Action.APPROVE_ROUTES: "route approval",
}


class OperationView(View):
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
        # container) and send `next`. Validated through the shared helper, so
        # the field cannot become an open redirect.
        destination = safe_next(request, fallback=reverse("control_plane:detail", kwargs={"key": key}))
        try:
            result = _web_operation(request, resource, self.action)
        except PolicyError as exc:
            messages.error(request, str(exc))
            return redirect(destination)
        verb = "Queued" if result["queued"] else "Already queued"
        phrase = OPERATION_PHRASE.get(self.action, self.action)
        messages.success(request, f"{verb} {phrase} for “{resource.key}”.")
        return redirect(destination)


class CertificateDownloadView(View):
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
        response["Content-Disposition"] = f'attachment; filename="{resource.key}-public.pem"'
        return response


class ResourceReportDownloadView(View):
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
        response["Content-Disposition"] = f'attachment; filename="{resource.key}-status.json"'
        return response


class ProviderSchemaView(View):
    def get(self, request):
        return JsonResponse(describe_providers(), json_dumps_params={"indent": 2})
