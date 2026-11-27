"""The uses of ``hq_sdk`` an extension must not be able to write, as mypy reads them. Never imported.

Each carries the error mypy reports for it, and ``warn_unused_ignores`` fails
the gate the day one stops being an error: a refusal is a promise only type
checking can hold. What an export accepts and returns is held by
``lint:sdk-breaking`` and by mypy over the extensions that call it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, override

from django import forms
from django.db import models
from django.http import HttpRequest, HttpResponse
from django.urls import reverse_lazy

from hq_sdk import (
    audit,
    capabilities,
    connections,
    demo,
    jobs,
    migrations,
    money,
    outbound,
    pages,
    plugin,
    reads,
    references,
    resources,
    tables,
    testing,
    ui,
    workflows,
    writes,
)


def the_clock(moment: datetime) -> None:
    reads.reached("2026-01-01")  # type: ignore[arg-type]
    reads.whole(moment)  # type: ignore[call-arg]
    reads.day_span(date(2026, 1, 1), last=3)  # type: ignore[arg-type]


def a_kinds_cadence(moment: datetime) -> None:
    # An extension declares how old a reading of its kind may get, and reads
    # it back through the host's one freshness.
    def weekly() -> reads.ReadCadence:
        return reads.ReadCadence(timedelta(days=1), timedelta(days=7))

    reads.freshness("example.readings", "2026-01-01")  # type: ignore[arg-type]
    reads.ReadCadence(timedelta(days=1), 7)  # type: ignore[arg-type]
    plugin.PluginIntegration(cadences=lambda: {"example.readings": weekly()})  # type: ignore[dict-item]


def a_held_clock(moment: datetime) -> None:
    testing.frozen(date(2026, 1, 1))  # type: ignore[arg-type]


@reads.derivation("example.count", reads=("example.Thing",))
def count(limit: int, *, open_only: bool = False) -> int:
    return limit if open_only else 0


def a_derivation() -> None:
    count("three")  # type: ignore[arg-type]
    count(3, closed=True)  # type: ignore[call-arg]
    reads.derivation("example.unread")  # type: ignore[call-arg]


def amounts() -> None:
    money.quantize_money(1.5)  # type: ignore[arg-type]
    money.money("1.00")  # type: ignore[arg-type]
    # A sign is the amount's own, written by money(); there is no sign to spell by hand.
    money.MINUS  # type: ignore[attr-defined]  # noqa: B018 - the access is the case


def signs(pace: float, beats: int, unread: float | None) -> None:
    money.signed_number("1.5", str)  # type: ignore[type-var]
    money.signed_number(pace, lambda minutes: minutes)  # type: ignore[arg-type,return-value]
    money.signed_number(pace, str, True)  # type: ignore[call-arg]


def measures(pace: float, beats: int, unread: float | None) -> None:
    ui.measured(pace, "mi", "2")  # type: ignore[arg-type]
    ui.measured(pace, "mi", 2, True)  # type: ignore[call-arg]
    ui.measured([pace], "mi")  # type: ignore[arg-type]


def sentences(registrar: str) -> None:
    ui.ended(None)  # type: ignore[arg-type]
    ui.status_pill("Renewing", tone=3)  # type: ignore[arg-type]
    ui.status_pill("+3 bpm", pill="no")  # type: ignore[arg-type]
    ui.status_pill("Allowed", dot="yes")  # type: ignore[arg-type]
    ui.status_pill("Tailnet", dot=True, ink="page")  # type: ignore[arg-type]
    ui.status_pill("1 serious", pill=False, icon=True)  # type: ignore[arg-type]
    ui.status_pill("django", chip="yes")  # type: ignore[arg-type]
    ui.Kpi("Net worth", "$1.00", tone="goood")  # type: ignore[arg-type]
    ui.Kpi("Notes", 3)  # type: ignore[arg-type]
    ui.ListRow("Group", actions=("Up",))  # type: ignore[arg-type]
    ui.ListRow("Backup host", snippet="nightly backup")  # type: ignore[arg-type]
    ui.ListRow("Backup host", chips="Running")  # type: ignore[arg-type]


def the_demo(due: date, at: datetime) -> None:
    demo.amount(Decimal(1))  # type: ignore[call-arg]


def a_demo_made_by_hand(at: datetime) -> None:
    audit.seeded_by_hand("/examples/notes/")  # type: ignore[call-arg]
    audit.seeded_by_hand("/examples/notes/", at="2026-01-01")  # type: ignore[arg-type]
    audit.seeded_by_hand("/examples/notes/", at)  # type: ignore[call-arg]


def importing(progress: jobs.Progress) -> dict[str, Any]:
    progress(percent="ten")  # type: ignore[arg-type]
    return {"created": 1}


def no_progress() -> dict[str, Any]:
    return {}


def work() -> None:
    jobs.reap()  # type: ignore[attr-defined]

    jobs.start("example.import", "Import", no_progress)  # type: ignore[arg-type]
    jobs.start("example.import", "Import")  # type: ignore[call-arg]


def as_someone(principal: capabilities.Principal) -> None:
    # Every one of these acts as someone, and says who.
    outbound.run_now("example.work")  # type: ignore[call-arg]
    capabilities.execute_capability("example.do", {})  # type: ignore[call-arg]
    resources.get_resource("example.things", 1)  # type: ignore[call-arg]
    resources.get_resource("example.things", 1, principal=principal, fields="name")  # type: ignore[arg-type]
    capabilities.execute_capability("example.do", ["x"], principal=principal)  # type: ignore[arg-type]


def may_use(spec: capabilities.CapabilitySpec, principal: capabilities.Principal) -> None:
    spec.authorize()  # type: ignore[call-arg]
    spec.permitted_to("example.write")  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class _ThingCommand:
    name: str = ""


def keeping(thing: models.Model) -> None:
    capabilities.keeps(lambda key: {})  # type: ignore[arg-type]
    capabilities.as_sent(thing, _ThingCommand)  # type: ignore[call-arg]
    capabilities.written(_ThingCommand(), ["name"])  # type: ignore[arg-type]


def from_a_form(request: HttpRequest) -> None:
    capabilities.execute_posted(request, "example.do")  # type: ignore[call-arg]
    capabilities.execute_posted(request, "example.do", {}, principal=None)  # type: ignore[call-arg]
    testing.form_keyed(["x"])  # type: ignore[arg-type]


def a_refused_answer(request: HttpRequest, form: forms.Form, answer: dict[str, Any]) -> HttpResponse:
    capabilities.refused(request, answer, form)  # type: ignore[call-arg]
    capabilities.refused(request, ["x"])  # type: ignore[arg-type]
    return capabilities.refusal_response(answer)


def a_refusal_a_person_reads(starts_on: str) -> None:
    capabilities.InputError()  # type: ignore[call-arg]


def a_person_holding() -> None:
    testing.holding(["example.read"])  # type: ignore[arg-type]


def catalogs(principal: capabilities.Principal) -> None:
    resources.describe_resources(principal)  # type: ignore[call-arg]
    resources.describe_resources(name=1)  # type: ignore[arg-type]
    connections.describe_connections(principal="example")  # type: ignore[arg-type]


def a_connection() -> None:
    connections.ConnectionInstance("example", "Example", "example", ui.Tone.GOOD, "Read")  # type: ignore[call-arg]


def looking_up(progress: jobs.Progress, *, subject: str, principal: capabilities.Principal) -> dict[str, Any]:
    return {}


def declared_words(spec: capabilities.CapabilitySpec) -> None:
    outbound.OutboundWork("example.sweep", "Sweep", "", "", looking_up, offered="no")  # type: ignore[arg-type]
    capabilities.CapabilitySpec(
        "example.note.set",
        "Keep a note.",
        "remote_write",  # type: ignore[arg-type]
        "example.write",
        Noting,
        noting,
    )

    outbound.ask("example.lookup.run", label="Look")  # type: ignore[call-arg]
    spec.words(1)  # type: ignore[arg-type]


class Note(models.Model):
    about = references.ReferenceField(heading="Notes")
    about_name = models.CharField(max_length=200)

    @override
    def __str__(self) -> str:
        return self.about_name


# A command is a frozen dataclass, which HQ closes to the fields it names.
@dataclass(frozen=True, slots=True)
class Noting:
    # A field says what it is, which records it names, or which choices it takes.
    note: Annotated[str, capabilities.Describe("What to write down.")]
    project: Annotated[str, capabilities.Names("example.projects"), capabilities.Describe("The project it is for.")]
    owner: Annotated[str, capabilities.Names("example.people", key="email")]
    tone: Annotated[str, capabilities.Choices((("plain", "Plain"), ("loud", "Loud")))]
    about: Annotated[str, capabilities.Refers(Note, "about")]


def noting(command: Noting, *, principal: capabilities.Principal) -> dict[str, Any]:
    return {}


def a_command_that_starts_somewhere() -> None:
    capabilities.CapabilitySpec(
        "example.notes.refresh",
        "Tidy notes.",
        capabilities.Effect.REMOTE_WRITE,
        "example.write",
        Noting,
        noting,
        about=lambda _principal: "Notes",  # type: ignore[arg-type, return-value]
    )

    capabilities.Describe()  # type: ignore[call-arg]
    capabilities.Describe("The project it is for.", api=None)  # type: ignore[arg-type]
    capabilities.Names(resource=1)  # type: ignore[arg-type]
    capabilities.Choices(("plain", "loud"))  # type: ignore[arg-type]
    capabilities.Refers("example.note", "about")  # type: ignore[arg-type]
    capabilities.CapabilitySpec(
        "example.pin.toggle",
        "Pin.",
        capabilities.Effect.REMOTE_WRITE,
        "example.write",
        Noting,
        noting,
        reached_from=None,  # type: ignore[arg-type]
    )


def a_page() -> None:
    pages.PageAction("Open")  # type: ignore[call-arg]
    # A hand-built action is a link: only a command's control posts, sends or looks destructive.
    pages.PageAction("Mark read", "/example/1/", method="post")  # type: ignore[call-arg]
    pages.PageAction("Remove", "/example/1/", danger=True)  # type: ignore[call-arg]
    pages.PageAction.command("example.review", label="Mark read")  # type: ignore[call-arg]
    pages.PageAction.post("example.block", sends=["client_id"])  # type: ignore[arg-type]
    # A press that posts opens no dialog, and a dialog posts nothing of its own.
    pages.PageAction.post("example.import", modal="import")  # type: ignore[call-arg]
    pages.PageAction.dialog("example.import", "import", sends={"key": "1"})  # type: ignore[call-arg]
    pages.page_context("Example", actions=["Open"])  # type: ignore[list-item]
    pages.page_context("Example", reads=1)  # type: ignore[arg-type]


def a_refused_action(link: workflows.ActionLink) -> None:
    workflows.ActionLink()  # type: ignore[call-arg]


def a_read_link() -> None:
    workflows.read_link("subject", "Example", "/example/", method="POST")  # type: ignore[call-arg]
    workflows.read_link("subject", "Example", "/example/", capabilities.Effect.READ)  # type: ignore[call-arg]


def a_dialog() -> None:
    pages.PageDialog("import", "Import records")  # type: ignore[call-arg]
    pages.PageAction("Import", "/example/import/", modal=3)  # type: ignore[arg-type]
    pages.PageAction.dialog("example.import", 3)  # type: ignore[arg-type]


class WronglyTrailed(pages.PageMixin):
    # One crumb written bare, not a tuple of them.
    page_trail = ("Things", reverse_lazy("example:things"))  # type: ignore[assignment]
    # A page reads under one capability of its own, never several.
    required_capability = ("example.read", "example.write")  # type: ignore[assignment]

    @override
    def trail_label(self, record: Any) -> int:  # type: ignore[override]
        return 1


def a_trail() -> None:
    pages.page_context("Example", trail=(("Things", 1),))  # type: ignore[arg-type]


class WronglyDeclared(tables.ListPageMixin):
    resource = "example.things"
    table_empty = None  # type: ignore[assignment]
    table_columns = ("Name",)  # type: ignore[assignment]


def a_column() -> None:
    tables.TableColumn("Note", drop="2")  # type: ignore[arg-type]


def the_deployment() -> None:
    testing.plain(2)  # type: ignore[arg-type]


def reading_a_page(response: HttpResponse) -> None:
    testing.text(3)  # type: ignore[arg-type]
    testing.select(response, "data-row-key", 3)  # type: ignore[arg-type]
    testing.elements(response)  # type: ignore[call-arg]
    testing.all_elements(response, "a")  # type: ignore[call-arg]
    testing.with_class(response)  # type: ignore[call-arg]


class Unnamed(writes.CapabilityCommandMixin):
    submits = 3  # type: ignore[assignment]


def a_manifest() -> None:
    item = plugin.NavigationItem("Notes", "example_notes:list", "example_notes", 40, "Example")
    group = plugin.NavigationGroup("Example", "Notes kept to show how a group is declared.")
    manifest = plugin.PluginManifest(
        id="example.notes",
        name="Example Notes",
        version="1.0.0",
        distribution="example-notes",
        source_repository="example/example-notes",
        source_workflow=".github/workflows/admit-plugin.yml",
        api_version=plugin.PLUGIN_API_VERSION,
        integration_provider="example_notes.plugin:integration",
        navigation=(item,),
        description="Notes, each with its author.",
        navigation_groups=(group,),
    )

    manifest.id = "example.other"  # type: ignore[misc]
    plugin.NavigationItem("Notes", "example_notes:list", "example_notes", "40")  # type: ignore[arg-type]
    plugin.PluginManifest(id="example.notes")  # type: ignore[call-arg]
    plugin.NavigationGroup("Example")  # type: ignore[call-arg]


def an_attention_url() -> None:
    plugin.PluginIntegration(attention=tuple, attention_url="/x/")  # type: ignore[arg-type]


def a_demo() -> None:
    plugin.PluginIntegration(demo_pages=("/examples/notes/first/",))  # type: ignore[arg-type]


def a_finding_on_the_queue() -> None:
    plugin.Finding(rule="example", subject="", severity="urgent", title="Example", explanation="")  # type: ignore[arg-type]
    plugin.Finding(rule="example", subject="", severity=ui.Tone.SERIOUS, title="Example")  # type: ignore[call-arg]


def a_section(hosts: int) -> None:
    ui.SectionPart("Pace", figure="yes")  # type: ignore[arg-type]
    ui.SectionPart("Hosts", foot=("Counted daily.",))  # type: ignore[arg-type]
    ui.SectionPart("Hosts", head=True)  # type: ignore[arg-type]
    ui.SectionPart("Hosts", moment="2026-11-01")  # type: ignore[arg-type]
    ui.SectionPart("Hosts", count=1.5)  # type: ignore[arg-type]
    ui.SectionPart("Hosts", template=("example/_hosts.html",))  # type: ignore[arg-type]


def an_allocation(cash: Decimal, owed: Decimal) -> None:
    ui.AllocationBar("Assets by kind", [ui.Share("Cash", 1.0, 1)])  # type: ignore[arg-type]
    ui.Share("Cash", 1.0, "one")  # type: ignore[arg-type]
    ui.Share("Cash", 1.0, 1, url=None)  # type: ignore[arg-type]


def a_list_with_its_own_fields(rows: Callable[..., dict[str, Any]]) -> None:
    resources.ListRead(rows, resources.BoundedQuery, dict, fields="name")  # type: ignore[arg-type]


def a_list_without_its_query_or_item(rows: Callable[..., dict[str, Any]]) -> None:
    resources.ListRead(rows)  # type: ignore[call-arg]


def a_list_with_the_deployment_ceiling(rows: Callable[..., dict[str, Any]]) -> None:
    resources.BoundedQuery(limit="many")  # type: ignore[arg-type]


def a_list_searched(rows: Callable[..., dict[str, Any]]) -> None:
    # A list's search says what every searched list's does, and may be left out.
    class Query(resources.BoundedQuery):
        query: resources.SearchText = None

    Query(query=["example"])  # type: ignore[arg-type]


def a_list_sorted_and_narrowed() -> None:
    resources.ListSort("name", "Name", "name")  # type: ignore[arg-type]
    resources.ListSort.both("name", 1)  # type: ignore[arg-type]
    resources.ListFilter("kind", "Kind", ("one",))  # type: ignore[arg-type]
    resources.ListToggle("stale", "Stale", "True for only the stale records.")  # type: ignore[call-arg]


def a_capability_renamed() -> None:
    migrations.RenameCapabilities(["example.entries"])  # type: ignore[arg-type]
