import json
from collections.abc import Iterable
from dataclasses import replace
from functools import cached_property
from typing import Any, override

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import Truncator
from django.views import View
from django.views.generic import TemplateView

from hq.platform.application.capabilities import capability_words, command_spec
from hq.platform.application.certificates import UPLOAD as CERTIFICATE_UPLOAD
from hq.platform.application.command_forms import command_form_class
from hq.platform.application.conditions import DRIFTED, holding
from hq.platform.application.contracts import Effect
from hq.platform.application.doors import NOBODY
from hq.platform.application.entity_links import entity_link, kind_label, page_url, record_name, web_url
from hq.platform.application.freshness import reading_age
from hq.platform.application.history import asked_by
from hq.platform.application.infrastructure import (
    serialize_resource,
)
from hq.platform.application.inventory import service_hostnames
from hq.platform.application.labels import field_label
from hq.platform.application.moments import moment
from hq.platform.application.pages import PageAction, RecordListMixin, RecordPageMixin, page_context
from hq.platform.application.payload_words import value_words
from hq.platform.application.references import Kind
from hq.platform.application.refusal_codes import Code
from hq.platform.application.refusal_status import refuse
from hq.platform.application.relationships import relationships_for
from hq.platform.application.resource_capabilities import (
    REMOVAL_CAPABILITY,
    Allowed,
    channels_inside,
    resource_capabilities,
)
from hq.platform.application.resource_context import (
    awaiting_approval,
    capabilities_of,
    controller_summary,
    record_list,
    stored,
)
from hq.platform.application.resource_operations import (
    ACTION_CAPABILITY,
    HISTORY_ROWS,
    READS_AGAIN,
    changes,
    supervision,
)
from hq.platform.application.resource_refusals import INFRASTRUCTURE
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.service_context import operation_history
from hq.platform.application.tables import Cell, DataTable, Group, Row, StatusMark, TableColumn
from hq.platform.application.topology_model import RELATIONS
from hq.platform.application.visit_refresh import page_visit
from hq.platform.application.web_writes import execute_posted
from hq.platform.application.work_runs import run_rows
from hq.platform.core.templatetags.action_tags import POST_FORM_ID
from hq.platform.core.web_refusals import refusal_response, refused
lazy from hq.platform.application.page_relations import for_container

from .cells import run
from .models import ApprovalRequest, ManagedResource, OperationRequest
from .provider_adapters.portainer import CONTAINER_KIND
from .provider_adapters.tls import CERTIFICATE_KIND
from .providers import PROVIDERS, describe_providers, read_spec, readout_rows, shown_spec
lazy from .container_views import container_detail


def _web_operation(request, resource, action) -> dict[str, Any]:
    """Ask for ``action`` on ``resource`` through its capability, as the operator."""

    return execute_posted(
        request, ACTION_CAPABILITY[action], {"reason": request.POST.get("reason", "").strip()}, target=resource.key
    )


def _removal_reason():
    """Why a record is removed, as its removal takes it. The confirmation
    button posts through the page's shared form (``POST_FORM_ID``), so the
    field joins that form by its ``form`` attribute."""

    form = command_form_class(command_spec(REMOVAL_CAPABILITY), fields=("reason",))()
    form.fields["reason"].widget.attrs["form"] = POST_FORM_ID
    return form


def _json(value: Any) -> str:
    """A stored document as JSON a person can read and paste, or "" for none."""

    return json.dumps(value, indent=2, sort_keys=True, default=str) if value else ""


def _spec_rows(
    kind: str, key: str, spec: dict[str, Any], rows: Iterable[tuple[str, str, str]], *, beside_readout: bool = False
) -> dict[str, tuple[tuple[str, str], ...]]:
    """A spec as an operator reads it before a destructive action, split the way
    the form splits it: fields by their model titles, an unset optional left out
    rather than shown as "None", and fields the provider declares routine folded.

    ``beside_readout`` is the record's own page, where the readout and the page's
    name stand above this: a value they already show is not repeated. ``rows``
    is its readout, ``(label, desired, observed)``.
    """

    provider = PROVIDERS[kind]
    fields = provider.spec_type.model_fields
    # What the readout above already printed. On anything with a handful of
    # fields the readout *is* the spec, and the disclosure would repeat it
    # whole.
    rows = tuple(rows)
    shown = {str(label).strip().casefold() for label, _, _ in rows}
    # A value the readout or the page's own name already shows is not said
    # again under a second label.
    said: set[str] = set()
    if beside_readout:
        said = {str(value) for _, desired, observed in rows for value in (desired, observed) if value}
        said.update(service_hostnames(kind, spec))
    primary: list[tuple[str, str]] = []
    advanced: list[tuple[str, str]] = []
    for name, value in shown_spec(kind, spec).items():
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
        if rendered == key or (rendered in said and not isinstance(value, bool)):
            continue
        row = (label, rendered)
        (advanced if name in provider.advanced_fields else primary).append(row)
    return {"primary": tuple(primary), "advanced": tuple(advanced)}


def _readout(record) -> tuple[tuple[str, str, str], ...]:
    """A record's readout as ``(label, desired, observed)``."""

    return tuple((row["label"], row["desired"], row["observed"]) for row in record["readout"])


def _linked_readout(record, relationships) -> tuple[tuple[str, str, str, tuple], ...]:
    """The readout, with each value that names a related entity as its link.

    A blank connection field is filled from the connections the relation graph
    says use this declaration.
    """

    related = {item.entity.label: item.entity for group in relationships.groups for item in group.items}
    connections = tuple(
        item.entity for group in relationships.groups for item in group.items if item.entity.kind == "connection"
    )
    field = PROVIDERS[record["kind"]].spec_type.model_fields.get("connection_ref")
    connection_label = (field.title or "") if field is not None else ""
    rows = []
    for label, desired, observed in _readout(record):
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
    # A confirmation page that cannot render is worse than one missing a
    # sentence, and this is the page an operator uses to stop.
    return read_spec(resource.kind, resource.spec, note) or ""


class ResourceRemoveView(View):
    """Ask first, then queue removal of the record itself.

    Not a row delete. What this describes lives at a provider, so dropping the
    declaration alone would leave the rewrite or proxy host in place with
    nothing in HQ pointing at it. HQ forgets its row only once a controller
    reports the provider is clear.
    """

    template_name = "control_plane/resource_confirm_remove.html"
    runs = (REMOVAL_CAPABILITY,)

    def get(self, request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        capabilities = resource_capabilities(resource)
        forget = capabilities.removal == "forget"
        removal = PageAction.command(REMOVAL_CAPABILITY, key, variant=capabilities.removal_variant, confirms=True)
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
                "spec_rows": _spec_rows(resource.kind, resource.key, resource.spec, readout_rows(resource)),
                "forget": forget,
                "holds_records": bool(PROVIDERS[resource.kind].contains),
                # The reason travels with the press, so it is asked only beside it.
                "removal_reason": _removal_reason() if removal is not None else None,
                "confirm": {
                    "action": removal,
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
        if refused(request, result):
            return redirect(page_url(Kind.RESOURCE, key))
        if "forgotten" in result:
            return redirect("control_plane:list")
        return redirect(page_url(Kind.RESOURCE, key))


class InfrastructureListView(RecordListMixin, TemplateView):
    template_name = "control_plane/resource_list.html"
    resource = INFRASTRUCTURE
    context_object_name = "resources"
    fields = ("key", "kind", "name", "summary", "standing", "origin_machine", "last_observed_at")
    page_title = "All records"

    @override
    def get_page_actions(self):
        return (
            PageAction.command("infrastructure.resource.create", primary=True),
            PageAction("Services", reverse("control_plane:services")),
            PageAction("Setting reference", reverse("control_plane:providers")),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        listed = changes(OperationRequest.objects.all(), 12)
        context["changes"] = [
            {"resource": operation.resource.key, "run": run}
            for operation, run in zip(listed, run_rows(listed), strict=True)
        ]
        context["provider_catalog"] = describe_providers()
        records = context["records"] = record_list(
            self.records,
            query=self.request.GET.get("q", ""),
            kind=self.request.GET.get("type", ""),
        )
        context["table"] = DataTable(
            "Records",
            RECORD_COLUMNS,
            groups=tuple(
                Group(group.label, tuple(Row(_record_cells(row)) for row in group.rows), count=len(group.rows))
                for group in records.groups
            ),
            region={"empty": "No records yet.", "filtered": records.filtered, "clear": reverse("control_plane:list")},
        )
        context["changed"] = DataTable(
            "Recent changes",
            CHANGE_COLUMNS,
            tuple(Row(_change_cells(change)) for change in context["changes"]),
        )
        return context


RECORD_COLUMNS = (
    TableColumn("Name", kind="identifier"),
    TableColumn("State", kind="status", css="key-col"),
    TableColumn("Reading", kind="status", css="date-col", drop=1),
)
CHANGE_COLUMNS = (
    TableColumn("Record", kind="identifier"),
    TableColumn("Change", drop=3),
    TableColumn("Result", css="key-col"),
    TableColumn("By", drop=2),
    TableColumn("Time", kind="status", css="date-col", drop=1),
)
# The characters of a change's note its row shows; its run's page says it whole.
_NOTE_SHOWN = 120


def _record_cells(row: dict) -> tuple[Cell, ...]:
    """A record's row: its name, what it is and the machine a proxy forwards
    to beneath; its state, and since when; and when it was last read."""

    machine = row["origin_machine"]
    standing = row["standing"]
    since = moment(standing.get("since"))
    lines = (
        *((Cell(row["summary"], muted=True, role="data"),) if row["summary"] else ()),
        *((Cell("on", muted=True, parts=(Cell.of(entity_link("machine", machine["name"])),)),) if machine else ()),
    )
    return (
        replace(Cell.of(entity_link("resource", row["key"], label=row["name"]), name=True), lines=lines),
        Cell(
            status=StatusMark(standing["label"], tone=standing["tone"], title=standing.get("detail", "")),
            lines=(Cell(moment=since, form="ago", muted=True),) if since else (),
        ),
        Cell(piece=reading_age(at=moment(row["last_observed_at"])), muted=True),
    )


def _change_cells(change: dict) -> tuple[Cell, ...]:
    """A change's row: its record, what was asked, how it ended, who asked and when."""

    asked = change["run"]
    note = Truncator(asked.note).chars(_NOTE_SHOWN)
    return (
        Cell.of(entity_link("resource", change["resource"])),
        Cell(asked.title),
        Cell(status=StatusMark(asked.word, tone=asked.tone), parts=(Cell(note),) if note else ()),
        Cell(asked.by.words),
        Cell(moment=asked.created_at, muted=True),
    )


class InfrastructureDetailView(RecordPageMixin, TemplateView):
    template_name = "control_plane/resource_detail.html"
    resource = INFRASTRUCTURE
    context_object_name = "resource"

    @cached_property
    def row(self) -> ManagedResource:
        """The record as stored, which what may be done to it is asked of."""

        return stored(self.record["key"])

    @cached_property
    def capabilities(self):
        return capabilities_of(self.row)

    @cached_property
    def container(self):
        """What the sweep knows about a watched container, and nothing otherwise."""

        if self.record["kind"] != CONTAINER_KIND:
            return None

        return container_detail(self.row, self.request)

    @override
    def get_page_title(self):
        return self.record["name"]

    @override
    def get_page_lede(self):
        # A record whose name is its type says what it does instead.
        kind = self.record["kind"]
        label = kind_label(kind)
        return PROVIDERS[kind].summary if label == self.get_page_title() else label

    @cached_property
    def all_relationships(self):
        """Every relation, for what the page derives from them (its home)."""

        return relationships_for(Kind.RESOURCE.of(self.record["key"]), principal=web_principal(self.request.user))

    @cached_property
    def relationships(self):
        """The relations the section shows: less what the page says elsewhere."""

        found = self.all_relationships
        if self.record["kind"] == CONTAINER_KIND:
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
        record = self.record
        if record["removal"]["pending"]:
            return ()
        key = record["key"]
        capabilities = self.capabilities
        drifted = bool(holding(record["conditions"], DRIFTED))
        provider = PROVIDERS[record["kind"]]
        actions = [
            verb_action(
                verb,
                key,
                allowed,
                origin=self.request.get_full_path(),
                primary=verb == "renew",
                # Applying a record that only reports on itself reads it again.
                variant=READS_AGAIN if verb == "reconcile" and provider.reported_fields else "",
            )
            for verb, allowed in capabilities.page_actions
        ]
        if drifted:
            # Changed outside HQ. Keeping that loses nothing, so it leads, and
            # restoring HQ's copy is the button beside it rather than the only one.
            actions.insert(
                0,
                PageAction.command(
                    "infrastructure.resource.accept_observed",
                    key,
                    primary=True,
                    query={"next": self.request.get_full_path()},
                ),
            )
        console = (
            web_url(provider.console({**record["spec"], **record["status"]}))
            if provider.console and provider.console_label
            else ""
        )
        if console:
            actions.insert(0, PageAction(provider.console_label, console, primary=not drifted))
        if gap := capabilities.write_gap:
            # Beside the verbs it holds back: what they need, at the step that gives it.
            actions.append(PageAction(gap.needs, gap.setup_url, title=gap.reason))
        if edit := capabilities.offered.edit(key, next=self.request.get_full_path()):
            actions.append(edit)
        if provider.material_form:
            replaces = "replace" if record["status"].get("certificate_available") else ""
            actions.append(PageAction.command(CERTIFICATE_UPLOAD, key, variant=replaces, primary=True))
        if capabilities.offered.removes:
            actions.append(PageAction.command(REMOVAL_CAPABILITY, key, variant=capabilities.removal_variant))
        return tuple(actions)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = self.record
        kind = record["kind"]
        context["page_visit"] = page_visit("resource", record["key"])
        context["controller"] = controller_summary(
            self.capabilities.actions, lambda verb: capability_words(ACTION_CAPABILITY[verb])
        )
        context["status"] = record["standing"]
        # A running container is summarized by what it runs, in its own band.
        if not (self.container and self.container["standing"]):
            context["page_aside"] = "control_plane/_record_status.html"
        # What this resource does, said by its own provider.
        context["label"] = kind_label(kind)
        # Relationships names the service it is for; the head says it only when
        # that section does not.
        relations = {group.phrase for group in self.relationships.groups}
        context["service_links"] = () if RELATIONS["declared_by"].inverse in relations else record["services"]
        # A DNS record answers with an address; a proxy sends requests on.
        context["origin_phrase"] = "Points to" if PROVIDERS[kind].answers else "Forwards to"
        context["managing_connections"] = tuple(
            item.entity
            for group in self.relationships.groups
            for item in group.items
            if item.entity.kind == "connection"
        )
        if self.container is not None:
            context["container"] = self.container
        # A change a credential asked for that nobody has answered, said on the
        # page an operator opens when a declaration has not moved.
        # Each said as who asked for it, from the audit row that opened it.
        held = awaiting_approval(record["key"])
        asked = asked_by(ApprovalRequest, held)
        context["awaiting_approval"] = tuple(asked.get(str(item.pk), NOBODY) for item in held)
        # Nothing for a container: its panel is the sweep's answer and a
        # container declares identity and nothing else.
        context["readout_rows"] = () if kind == CONTAINER_KIND else _linked_readout(record, self.relationships)
        context["relationships"] = self.relationships
        context["spec_rows"] = _spec_rows(kind, record["key"], record["spec"], _readout(record), beside_readout=True)
        history = operation_history(run_rows(changes(self.row.operations.all(), HISTORY_ROWS)))
        context["history"] = (history,) if history else ()
        context["spec_json"] = _json(record["spec"])
        context["diagnostic_status"] = _json(record["status"])
        context["installed"] = DataTable.of("Installed on", CONSUMER_COLUMNS, record.get("consumers") or ())
        context["saved"] = DataTable.of(
            "Saved to 1Password", SAVED_COLUMNS, record["status"].get("published_facts") or ()
        )
        return context


def _consumer(consumer: dict) -> Cell:
    """Where a certificate is installed, and the connection that installs it there."""

    through = consumer["connection_ref"]
    return Cell(
        consumer["name"],
        consumer["url"],
        name=True,
        lines=(Cell("through", muted=True, parts=(Cell.of(entity_link("connection", through)),)),) if through else (),
    )


def _saved(publication: dict) -> Cell:
    """Whether the last reconcile saved a certificate's facts to its item, and which."""

    if publication.get("detail"):
        return Cell("Not saved", tone="serious", lines=(Cell(publication["detail"], muted=True),))
    if not publication.get("written"):
        return Cell("Already up to date", tone="good")
    fields = [
        *(("certificate", "key") if publication.get("material") == "written" else ()),
        *(field.lower() for field in publication.get("fields") or ()),
    ]
    return Cell("Saved", tone="good", lines=(Cell(", ".join(fields), muted=True),) if fields else ())


CONSUMER_COLUMNS = (
    TableColumn("Place", kind="identifier", cell=_consumer),
    TableColumn(
        "Names checked",
        kind="identifier",
        cell=lambda consumer: run(Cell.of(entity_link("service", domain)) for domain in consumer["domains"]),
    ),
)
SAVED_COLUMNS = (
    TableColumn("Item", kind="identifier", cell=lambda publication: Cell(publication["target"], name=True)),
    TableColumn("Last saved", kind="status", cell=_saved),
)


def is_destructive(verb: str) -> bool:
    """Whether a resource's verb takes something away from whoever is using it,
    as its capability declares: asked for by name on a page of its own, never
    pressed straight from a head."""

    return command_spec(ACTION_CAPABILITY[verb]).effect is Effect.DESTRUCTIVE


def verb_action(
    verb: str, key: str, allowed: Allowed, *, origin: str, primary: bool = False, variant: str = ""
) -> PageAction | None:
    """One of a resource's verbs as a head control, unusable with the reason
    it is not ``allowed``. A destructive one opens its confirmation, named,
    which comes back to ``origin``; the rest post at once."""

    refusal = "" if allowed.enabled else allowed.reason
    back = {"next": origin} if is_destructive(verb) else None
    return PageAction.command(
        ACTION_CAPABILITY[verb], key, variant=variant, primary=primary, query=back, refused=refusal
    )


class OperationView(View):
    """Ask the controller for one action on one resource.

    The action comes from the URL rather than from the class, so adding a verb
    is a route and a phrase rather than another view that does what this one
    already does.
    """

    action = OperationRequest.Action.RECONCILE

    @property
    def runs(self) -> tuple[str, ...]:
        return (ACTION_CAPABILITY[self.action],)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # A destructive verb's confirmation is a page of its own; any other verb only posts.
        self.http_method_names = ["get", "post", "options"] if is_destructive(self.action) else ["post", "options"]

    def get(self, request, key):
        """A destructive verb's confirmation, naming what stops."""

        resource = get_object_or_404(ManagedResource, key=key)
        name = record_name(resource.kind, resource.spec, resource.key)
        label = f"{capability_words(ACTION_CAPABILITY[self.action])} {name}"
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
                    "action": PageAction.command(
                        ACTION_CAPABILITY[self.action], key, query=request.GET.dict(), confirms=True
                    ),
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
        destination = safe_next(request, fallback=page_url(Kind.RESOURCE, key))
        refused(request, _web_operation(request, resource, self.action))
        return redirect(destination)


class CertificateDownloadView(View):
    def get(self, _request, key):
        resource = get_object_or_404(ManagedResource, key=key)
        certificate_pem = resource.status.get("certificate_pem", "")
        if resource.kind != CERTIFICATE_KIND or not certificate_pem:
            return refusal_response(refuse(Code.NOT_FOUND, "No verified certificate to download."))
        if "PRIVATE KEY-----" in certificate_pem:
            # HQ stored something it should never have: its fault, never the asker's.
            return refusal_response(refuse(Code.OPERATION_FAILED, "The certificate file contains a private key."))
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
