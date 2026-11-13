"""An extension's use of ``hq_sdk``, as mypy reads it. Never imported.

The test that every export is annotated is satisfied by ``Any``, which checks
nothing. This holds the annotations to what they promise, from the side an
extension sees: each valid call pins what it returns with ``assert_type``, so
a return that slides to ``Any`` fails, and each call an extension must not be
able to make carries the error mypy reports for it. ``warn_unused_ignores``
fails the gate the day one of those stops being an error.
"""

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, assert_type, override

from django import forms
from django.db import models
from django.http import HttpRequest, HttpResponse
from django.urls import reverse_lazy
from django.utils.functional import Promise
from django.utils.safestring import SafeString

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
    site,
    tables,
    testing,
    ui,
    validation,
    workflows,
    writes,
)


def the_clock(moment: datetime) -> None:
    assert_type(reads.today(), date)
    assert_type(reads.present(), datetime)
    assert_type(reads.reached(moment), bool)
    assert_type(reads.passed(moment), bool)
    assert_type(reads.since(moment), timedelta)
    assert_type(reads.whole(moment, timedelta(days=1)), int)
    assert_type(reads.day_span(date(2026, 1, 1)), tuple[datetime, datetime | None])
    assert_type(reads.read_once("example.key", lambda: 3), int)

    reads.reached("2026-01-01")  # type: ignore[arg-type]
    reads.whole(moment)  # type: ignore[call-arg]
    reads.day_span(date(2026, 1, 1), last=3)  # type: ignore[arg-type]


def a_held_clock(moment: datetime) -> None:
    # A test holds the clock every derivation and reads.present() read.
    with testing.frozen(moment) as held:
        assert_type(held, datetime)
    with testing.frozen():
        assert_type(reads.present(), datetime)

    testing.frozen(date(2026, 1, 1))  # type: ignore[arg-type]


@reads.derivation("example.count", reads=("example.Thing",))
def count(limit: int, *, open_only: bool = False) -> int:
    return limit if open_only else 0


def a_derivation() -> None:
    # A derived function keeps its whole signature, not only what it returns.
    assert_type(count(3), int)
    assert_type(count(3, open_only=True), int)

    count("three")  # type: ignore[arg-type]
    count(3, closed=True)  # type: ignore[call-arg]
    reads.derivation("example.unread")  # type: ignore[call-arg]


def amounts() -> None:
    assert_type(money.to_money("1.20"), Decimal | None)
    assert_type(money.to_money("1.20", Decimal(0)), Decimal)
    assert_type(money.quantize_money(Decimal("1.234")), Decimal)
    assert_type(money.money(Decimal(1)), str)
    assert_type(money.money(Decimal(-1), cents=False, plus=True), str)

    money.quantize_money(1.5)  # type: ignore[arg-type]
    money.money("1.00")  # type: ignore[arg-type]
    # A sign is the amount's own, written by money(); there is no sign to spell by hand.
    money.MINUS  # type: ignore[attr-defined]  # noqa: B018 - the access is the case


def signs(pace: float, beats: int, unread: float | None) -> None:
    # The figure is written from the value's own kind, so it can use its format specs.
    assert_type(money.signed_number(pace, lambda minutes: f"{minutes:.1f} min", plus=True), str)
    assert_type(money.signed_number(-beats, lambda drop: f"{drop:,d} bpm"), str)
    assert_type(money.signed_number(Decimal("-1.5"), lambda amount: f"{amount:.2f}%"), str)
    assert_type(money.signed_number(unread, str), str)

    money.signed_number("1.5", str)  # type: ignore[type-var]
    money.signed_number(pace, lambda minutes: minutes)  # type: ignore[arg-type,return-value]
    money.signed_number(pace, str, True)  # type: ignore[call-arg]


def sentences(registrar: str) -> None:
    assert_type(ui.ended(f"Renews through {registrar}"), str)

    ui.ended(None)  # type: ignore[arg-type]
    assert_type(ui.dotted(registrar, 3, None), str)
    assert_type(ui.status_pill("Renewing", registrar, tone="attention", title=registrar), SafeString)
    ui.status_pill("Renewing", tone=3)  # type: ignore[arg-type]
    # The same state as its words in its tone, with no pill around them.
    assert_type(ui.status_pill("+3 bpm", tone="good", pill=False), SafeString)
    ui.status_pill("+3 bpm", pill="no")  # type: ignore[arg-type]
    # A standing state, its words after a steady dot in its tone.
    assert_type(ui.status_pill("Allowed", tone="good", dot=True), SafeString)
    ui.status_pill("Allowed", dot="yes")  # type: ignore[arg-type]
    # A heading's own words after the dot, in the ink around them.
    assert_type(ui.status_pill("Tailnet", tone="good", dot=True, ink="context"), SafeString)
    ui.status_pill("Tailnet", dot=True, ink="page")  # type: ignore[arg-type]
    # A cell's state led by its mark, in its tone.
    assert_type(ui.status_pill("1 serious", tone="serious", pill=False, icon="shield"), SafeString)
    ui.status_pill("1 serious", pill=False, icon=True)  # type: ignore[arg-type]
    assert_type(ui.status_pill("django", chip=True), SafeString)
    ui.status_pill("django", chip="yes")  # type: ignore[arg-type]
    assert_type(ui.Kpi("Net worth", "$1.00", "+$1.00", tone="good", detail_url="/x/").holds_links, bool)
    ui.ListRow("Group", actions=("Up",))  # type: ignore[arg-type]
    # A search hit's snippet is runs of the record's text, each with whether it matched.
    ui.ListRow("Backup host", snippet=(("nightly ", False), ("backup", True)))
    ui.ListRow("Backup host", snippet="nightly backup")  # type: ignore[arg-type]
    # A place's chips are words, one each.
    ui.ListRow("Backup host", url="/x/", chips=("Running",))
    ui.ListRow("Backup host", chips="Running")  # type: ignore[arg-type]
    assert_type(validation.sentence_closes("src"), list[str])
    assert_type(validation.sentence_labels("src"), list[str])


def the_demo(due: date, at: datetime) -> None:
    # A substitution returns what it was given, moved or not.
    assert_type(demo.day(due), date)
    assert_type(demo.day(at), datetime)
    assert_type(demo.day(None), None)
    assert_type(demo.amount(Decimal(1), key="example"), Decimal)
    assert_type(demo.label("Example", key="example"), str)
    assert_type(demo.showing_demo(), bool)

    demo.amount(Decimal(1))  # type: ignore[call-arg]


def a_demo_made_by_hand(at: datetime) -> None:
    with audit.seeded_by_hand("/examples/notes/", at=at):
        pass

    audit.seeded_by_hand("/examples/notes/")  # type: ignore[call-arg]
    audit.seeded_by_hand("/examples/notes/", at="2026-01-01")  # type: ignore[arg-type]
    audit.seeded_by_hand("/examples/notes/", at)  # type: ignore[call-arg]


def importing(progress: jobs.Progress) -> dict[str, Any]:
    progress("Reading the archive", 10)
    progress(force=True)
    progress(percent="ten")  # type: ignore[arg-type]
    return {"created": 1}


def no_progress() -> dict[str, Any]:
    return {}


def work() -> None:
    job = jobs.start("example.import", "Import", importing)
    assert_type(job, jobs.Job)
    assert_type(job.seen_state, str)
    assert_type(job.seen_state_display, str)
    assert_type(job.seen_error, str)
    jobs.reap()  # type: ignore[attr-defined]

    jobs.start("example.import", "Import", no_progress)  # type: ignore[arg-type]
    jobs.start("example.import", "Import")  # type: ignore[call-arg]


def as_someone(principal: capabilities.Principal) -> None:
    # The tailnet device an agent's call came from, or "" where HQ placed none.
    assert_type(principal.device, str)
    assert_type(outbound.run_now("example.work", principal=principal), jobs.Job)
    assert_type(
        capabilities.execute_capability("example.do", {"name": "x"}, principal=principal),
        dict[str, Any],
    )
    page = resources.list_resource("example.things", principal=principal)
    assert_type(page["items"], list[dict[str, Any]])
    assert_type(page["count"], int)
    assert_type(resources.get_resource("example.things", 1, principal=principal), dict[str, Any])
    assert_type(
        resources.list_resource("example.things", principal=principal, fields=["name", "parts.name"])["items"],
        list[dict[str, Any]],
    )
    assert_type(resources.get_resource("example.things", 1, principal=principal, fields=("name",)), dict[str, Any])

    # Every one of these acts as someone, and says who.
    outbound.run_now("example.work")  # type: ignore[call-arg]
    capabilities.execute_capability("example.do", {})  # type: ignore[call-arg]
    resources.get_resource("example.things", 1)  # type: ignore[call-arg]
    resources.get_resource("example.things", 1, principal=principal, fields="name")  # type: ignore[arg-type]
    capabilities.execute_capability("example.do", ["x"], principal=principal)  # type: ignore[arg-type]


def from_a_form(request: HttpRequest) -> None:
    # A view's write runs as the signed-in person, under the key their form posted.
    assert_type(capabilities.execute_posted(request, "example.do", {"name": "x"}), dict[str, Any])
    assert_type(
        capabilities.execute_posted(request, "example.do", {}, target=1, expected_updated_at="x"), dict[str, Any]
    )
    assert_type(testing.form_keyed({"name": "x"}), dict[str, Any])
    assert_type(testing.form_keyed(), dict[str, Any])

    capabilities.execute_posted(request, "example.do")  # type: ignore[call-arg]
    capabilities.execute_posted(request, "example.do", {}, principal=None)  # type: ignore[call-arg]
    testing.form_keyed(["x"])  # type: ignore[arg-type]


def a_refused_answer(request: HttpRequest, form: forms.Form, answer: dict[str, Any]) -> HttpResponse:
    # A refusal is said on the form or the next page, or is the whole response.
    refusal = capabilities.refused(request, answer, form=form)
    if refusal is not None:
        assert_type(refusal.message, str)
        assert_type(refusal.status, int)
    capabilities.refused(request, answer, form)  # type: ignore[call-arg]
    capabilities.refused(request, ["x"])  # type: ignore[arg-type]
    return capabilities.refusal_response(answer)


def a_person_holding() -> None:
    # A test asks the real page gate as a person holding only the capabilities it names.
    assert_type(testing.holding("example.read", "example.write"), AbstractContextManager[Any])
    with testing.holding():
        pass

    testing.holding(["example.read"])  # type: ignore[arg-type]


def catalogs(principal: capabilities.Principal) -> None:
    # Each describes alone, or says for ``principal`` whether each entry is permitted.
    assert_type(resources.describe_resources()["resources"][0]["name"], str)
    assert_type(resources.describe_resources(principal=principal)["resources"][0]["permitted"], bool)
    # One kind, with its list's whole query schema.
    assert_type(resources.describe_resources(name="example.records")["schema_version"], int)
    assert_type(connections.describe_connections()["schema_version"], int)
    assert_type(connections.describe_connections(principal=principal)["connections"][0]["abilities"][0]["name"], str)

    resources.describe_resources(principal)  # type: ignore[call-arg]
    resources.describe_resources(name=1)  # type: ignore[arg-type]
    connections.describe_connections(principal="example")  # type: ignore[arg-type]


def a_connection() -> None:
    # A connection says what kind of credential it holds, a keyless one included.
    keyless = connections.ConnectionInstance("example", "Example", "example", "good", "Read", credential_model="none")
    assert_type(keyless.credential_model, str)
    assert_type(keyless.keyless, bool)

    connections.ConnectionInstance("example", "Example", "example", "good", "Read")  # type: ignore[call-arg]


def looking_up(progress: jobs.Progress, *, subject: str, principal: capabilities.Principal) -> dict[str, Any]:
    return {}


def declared_words(spec: capabilities.CapabilitySpec) -> None:
    # What a command's button and toast say is read from its declaration.
    lookup = outbound.OutboundWork(
        "example.lookup", "Look up a note", "Ask.", "example.write", looking_up, verb="Look up"
    )
    assert_type(lookup.words, str)
    assert_type(spec.words(), str)
    assert_type(spec.words("all"), str)
    assert_type(spec.requested("example"), str)
    # What asking for it wants done, read after "wants to".
    capabilities.CapabilitySpec(
        "example.keep",
        "Keep a note.",
        "local_write",
        "example.write",
        Noting,
        noting,
        "key",
        wants="keep the note {target}",
    )
    assert_type(spec.wants, str)
    assert_type(spec.wanted(), str)
    assert_type(spec.wanted("on"), str)

    outbound.ask("example.lookup", label="Look")  # type: ignore[call-arg]
    spec.words(1)  # type: ignore[arg-type]


class Note(models.Model):
    about = references.ReferenceField(heading="Notes")
    about_name = models.CharField(max_length=200)

    @override
    def __str__(self) -> str:
        return self.about_name


class Noting(capabilities.StrictCommand):
    # A field says what it is, which records it names, or which choices it takes.
    note: Annotated[str, capabilities.Describe("What to write down.")]
    project: Annotated[str, capabilities.Names("example.projects"), capabilities.Describe("The project it is for.")]
    owner: Annotated[str, capabilities.Names("example.people", key="email")]
    tone: Annotated[str, capabilities.Choices((("plain", "Plain"), ("loud", "Loud")))]
    about: Annotated[str, capabilities.Refers(Note, "about")]


def noting(command: Noting, *, principal: capabilities.Principal) -> dict[str, Any]:
    return {}


def a_command_that_starts_somewhere() -> None:
    # Where a person starts a command is its page's route, or the page of what it acts on.
    started = capabilities.CapabilitySpec(
        "example.note", "Write a note.", "local_write", "example.write", Noting, noting, reached_from="example:notes"
    )
    assert_type(started.reached_from, str)
    assert_type(capabilities.ON_ITS_PAGE, str)
    # A switch whose state no record holds declares where it stands.
    capabilities.CapabilitySpec(
        "example.quiet", "Quiet notes.", "local_write", "example.write", Noting, noting, state=lambda: False
    )
    # What its page says of it beside its form, read as the page is drawn.
    about = capabilities.CommandAbout("Notes", (capabilities.CommandFact("Kept for", "A year", ("Then archived.",)),))
    described = capabilities.CapabilitySpec(
        "example.tidy", "Tidy notes.", "local_write", "example.write", Noting, noting, about=lambda: about
    )
    assert_type(described.about, Callable[[], capabilities.CommandAbout] | None)
    assert_type(about.facts[0].detail, tuple[str, ...])
    capabilities.CapabilitySpec(
        "example.tidy",
        "Tidy notes.",
        "local_write",
        "example.write",
        Noting,
        noting,
        about=lambda: "Notes",  # type: ignore[arg-type, return-value]
    )
    capabilities.CapabilitySpec(
        "example.pin",
        "Pin a note.",
        "local_write",
        "example.write",
        Noting,
        noting,
        reached_from=capabilities.ON_ITS_PAGE,
    )
    assert_type(capabilities.Names("example.projects").key, str)
    assert_type(capabilities.Choices({"plain": "Plain"}.items()).options, tuple[tuple[str, str], ...])

    # What only a client reading JSON needs is the second part, which a form leaves off.
    assert_type(capabilities.Describe("The project it is for.", api="Its slug.").api, str)
    capabilities.Describe()  # type: ignore[call-arg]
    capabilities.Describe("The project it is for.", api=None)  # type: ignore[arg-type]
    capabilities.Names(resource=1)  # type: ignore[arg-type]
    capabilities.Choices(("plain", "loud"))  # type: ignore[arg-type]
    assert_type(capabilities.Refers(Note, "about").field, str)
    capabilities.Refers("example.note", "about")  # type: ignore[arg-type]
    capabilities.CapabilitySpec(
        "example.pin",
        "Pin.",
        "local_write",
        "example.write",
        Noting,
        noting,
        reached_from=None,  # type: ignore[arg-type]
    )


def a_page() -> None:
    assert_type(pages.page_context("Example"), dict[str, pages.Page])
    assert_type(pages.PageAction("Open", "/example/", primary=True).css, str)

    pages.PageAction("Open")  # type: ignore[call-arg]
    # A POST action names the field its value is sent as, for a row's actions.
    assert_type(pages.PageAction("Mark read", "/example/1/", method="post", value="read", field="status").field, str)
    pages.PageAction("Mark read", "/example/1/", field=1)  # type: ignore[arg-type]
    # A POST action may send more than its button's field: what it acts on.
    pages.PageAction("Block", "/example/block/", method="post", field="blocked", value="1", fields={"client_id": "a"})
    pages.PageAction("Block", "/example/block/", method="post", fields=["client_id"])  # type: ignore[arg-type]
    pages.page_context("Example", actions=["Open"])  # type: ignore[list-item]
    assert_type(pages.page_context("Example", reads=("example.things",)), dict[str, pages.Page])
    pages.page_context("Example", reads=1)  # type: ignore[arg-type]


def a_refused_action() -> None:
    # A link the command would refuse now says why, and is drawn unusable.
    link = workflows.ActionLink("run", "Run", "remote_write", "/run/", refusal="Not now.")
    assert_type(link.refusal, str)

    workflows.ActionLink("run", "Run", "remote_write", "/run/", refusal=True)  # type: ignore[arg-type]


def a_page_lead(page: pages.Page) -> None:
    assert_type(page.lead, pages.PageAction | None)


def an_empty_table(view: tables.TableListMixin) -> None:
    assert_type(view.get_table_empty(), str)
    assert_type(view.get_table_empty_remedy(), tables.Remedy | None)
    assert_type(tables.Remedy("Connect a bank", "/connections/").url, str)


def a_dialog() -> None:
    dialog = pages.PageDialog("import", "Import records", "example/_import_form.html", lede="Known files are skipped.")
    action = pages.PageAction("Import", "/example/import/", primary=True, modal=dialog)
    assert_type(action.dialog, pages.PageDialog | None)
    assert_type(action.modal_id, str)
    assert_type(pages.page_context("Example", actions=(action,))["page"].dialogs, tuple[pages.PageDialog, ...])

    pages.PageDialog("import", "Import records")  # type: ignore[call-arg]
    pages.PageAction("Import", "/example/import/", modal=3)  # type: ignore[arg-type]


class Things(tables.TableListMixin, pages.PageMixin):
    # What a list declares, held to the types the host reads them as.
    page_title = "Things"
    table_empty = "No things yet."
    table_empty_why = "Add one in the vault."
    table_columns = (
        tables.TableColumn("Name", "name", kind="identifier"),
        tables.TableColumn("Note", kind="prose", drop=1),
    )
    table_search_fields = ("name",)
    table_default_sort = "name"
    # An identifier beside a wide prose column takes what it leaves.
    table_room = "shared"


class Projections(tables.TableListMixin):
    table_columns = (tables.TableColumn("Name", kind="identifier"),)
    notes: models.QuerySet[Note]

    @override
    def get_table_rows(self, query: tables.TableQuery) -> tables.Projected:
        rows = self.notes.filter(about_name__in=query.chosen("name")).values_list("pk", flat=True)
        return tables.Projected(rows, lambda page: [str(pk) for pk in page])


def a_projected_list(view: Projections, query: tables.TableQuery) -> None:
    assert_type(view.get_table_rows(query), tables.Projected)
    assert_type(query.chosen("state"), tuple[str, ...])
    assert_type(query.search, str)
    assert_type(view.get_table_rows(query)[0:40], list[Any])
    tables.Projected([], lambda page: page)  # type: ignore[arg-type]


class Refreshed(pages.PageMixin):
    """A page action may be an ``ask``, the control that asks for declared work."""

    @override
    def get_page_actions(self) -> tuple[pages.PageAction | pages.Ask, ...]:
        return (outbound.ask("example.lookup", primary=True), pages.PageAction("Things", "/example/things/"))


def an_asked_action(page: Refreshed) -> None:
    assert_type(outbound.ask("example.lookup"), pages.Ask)
    assert_type(page.get_page_actions(), tuple[pages.PageAction | pages.Ask, ...])


class AThing(pages.PageMixin):
    # A record's page two levels down: the pages above it, then the record's own.
    page_trail = (("Example", reverse_lazy("example:home")), ("Things", reverse_lazy("example:things")))

    @override
    def trail_label(self, record: Any) -> str | None:
        return str(record)


class WronglyTrailed(pages.PageMixin):
    # One crumb written bare, not a tuple of them.
    page_trail = ("Things", reverse_lazy("example:things"))  # type: ignore[assignment]

    @override
    def trail_label(self, record: Any) -> int:  # type: ignore[override]
        return 1


def a_trail(page: AThing) -> None:
    assert_type(page.get_page_trail(), tuple[tuple[str, str | Promise], ...])
    # A function view passes the same lazy crumbs a class declares.
    assert_type(
        pages.page_context("Example", trail=AThing.page_trail)["page"].trail, tuple[tuple[str, str | Promise], ...]
    )
    pages.page_context("Example", trail=(("Things", reverse_lazy("example:things")),))
    pages.page_context("Example", trail=(("Things", 1),))  # type: ignore[arg-type]
    # An override names the crumb by the SDK's own name.
    crumb: pages.Crumb = ("Things", reverse_lazy("example:things"))
    assert_type(pages.page_context("Example", trail=(crumb,))["page"].trail, tuple[pages.Crumb, ...])


class WronglyDeclared(tables.TableListMixin):
    table_empty = None  # type: ignore[assignment]
    table_columns = ("Name",)  # type: ignore[assignment]
    table_room = "wide"  # type: ignore[assignment]


def a_column() -> None:
    assert_type(tables.TableColumn("Cost", kind="numeric").kind, str)
    assert_type(tables.TableColumn("Note", drop=2).drop, int)
    tables.TableColumn("Note", drop="2")  # type: ignore[arg-type]


def the_deployment() -> None:
    assert_type(site.host(), str)
    assert_type(testing.plain(b"2\xc2\xa0weeks"), str)
    assert_type(testing.plain("2 weeks"), str)

    testing.plain(2)  # type: ignore[arg-type]


class Renamed(writes.ServiceCreateMixin):
    """A write view rewords what it says by overriding the defaults it inherits."""

    created_message = "{noun} {target} added."


class Welcoming(writes.WriteMessages):
    """A mixin that rewords a message for several views subclasses the root,
    so it wins in whichever order a view writes its bases."""

    created_message = "{noun} {target} is in."


class WelcomedFirst(Welcoming, writes.ServiceCreateMixin):
    pass


class WelcomedLast(writes.ServiceCreateMixin, Welcoming):
    pass


class Miscounted(writes.WriteMessages):
    created_message = 3  # type: ignore[assignment]


class Unsaid(writes.ServiceDeleteMixin):
    deleted_message = None  # type: ignore[assignment]


def saving(command: Any, *, principal: Any) -> dict[str, Any]:
    return {}


class Saved(writes.CommandFormMixin):
    """A command view names its service as a staticmethod, which the declaration takes."""

    service = staticmethod(saving)
    success_message = "Saved."


class Unserviced(writes.CommandFormMixin):
    service = "saving"  # type: ignore[assignment]


def announcements() -> None:
    assert_type(writes.ServiceWriteMixin.created_message, str)
    assert_type(writes.ServiceWriteMixin.updated_message, str)
    assert_type(writes.ServiceWriteMixin.deleted_message, str)
    assert_type(writes.ServiceWriteMixin.identity_result_key, str)
    assert_type(Renamed.created_message, str)
    assert_type(writes.WriteMessages.created_message, str)
    assert_type(WelcomedFirst.created_message, str)
    assert_type(WelcomedLast.created_message, str)


def a_manifest() -> None:
    # Every extension builds its navigation positionally, so a manifest class
    # that took keywords alone would break each one.
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
    assert_type(manifest.navigation, tuple[plugin.NavigationItem, ...])
    assert_type(manifest.navigation_groups, tuple[plugin.NavigationGroup, ...])
    assert_type(manifest.description, str)
    assert_type(manifest.id, str)

    manifest.id = "example.other"  # type: ignore[misc]
    plugin.NavigationItem("Notes", "example_notes:list", "example_notes", "40")  # type: ignore[arg-type]
    plugin.PluginManifest(id="example.notes")  # type: ignore[call-arg]
    plugin.NavigationGroup("Example")  # type: ignore[call-arg]


def an_attention_url() -> None:
    # Where an extension's attention items are decided, asked for when a page links there.
    integration = plugin.PluginIntegration(attention=tuple, attention_url=lambda: "/x/")
    assert_type(integration.attention_url, Callable[[], str] | None)

    plugin.PluginIntegration(attention=tuple, attention_url="/x/")  # type: ignore[arg-type]


def a_demo() -> None:
    # The records the showcase writes for an extension, and a page of each kind.
    def plant(owner: object) -> None:
        return None

    integration = plugin.PluginIntegration(demo_seed=plant, demo_pages=lambda: ("/examples/notes/first/",))
    assert_type(integration.demo_pages, Callable[[], Iterable[str]] | None)

    plugin.PluginIntegration(demo_pages=("/examples/notes/first/",))  # type: ignore[arg-type]


def a_gated_insight() -> None:
    # An item about what only some capabilities may read names them; left
    # out, every reader of the queue is shown it.
    gated = ui.Insight("attention", "Finding", "Example is not answering", "", "", read_capabilities=("example.read",))
    assert_type(gated.read_capabilities, tuple[str, ...])
    ui.Insight("attention", "Finding", "Example", "", "", read_capabilities="example.read")  # type: ignore[arg-type]


def an_insight_about_one_part() -> None:
    # An item about one of its subject's many things names that thing, so a
    # page about it alone leaves out its neighbors' items.
    about = ui.Insight("attention", "Finding", "Example is restarting", "", "", part="example-web")
    assert_type(about.part, str)
    ui.Insight("attention", "Finding", "Example", "", "", part=("example-web",))  # type: ignore[arg-type]


def a_section(hosts: int) -> None:
    # A page built in code draws the section a template writes as {% section %}:
    # its body from a template, or its empty line when it has none.
    part = ui.SectionPart("Hosts", count=hosts, template="example/_hosts.html", context={"hosts": ()})
    assert_type(part.__html__(), SafeString)
    assert_type(part.heading_id, str)
    ui.SectionPart("Hosts", empty="No host yet.", fold=True, data={"fragment": "hosts"})
    # Its empty line offers the link that fills it; its heading may name a
    # moment, or be one.
    ui.SectionPart("Hosts", empty="No host yet.", remedy_label="Add a host", remedy_url="/example/hosts/new/")
    ui.SectionPart(moment=date(2026, 11, 1), when="month", template="example/_month.html")
    # A tile that stands on its own draws its head inside its frame.
    ui.SectionPart("Hosts", head="above", template="example/_hosts.html", context={"hosts": ()})
    # Its foot says how to read the body, its aside links to the whole, and a
    # drawing is a figure whose head is its caption.
    ui.SectionPart("Hosts", foot="Counted daily.", template="example/_hosts.html", context={"hosts": ()})
    ui.SectionPart("Hosts", more_label="View hosts", more_url="/example/hosts/", template="example/_hosts.html")
    ui.SectionPart("Pace", figure=True, template="example/_pace.html")
    ui.SectionPart("Pace", figure="yes")  # type: ignore[arg-type]
    ui.SectionPart("Hosts", foot=("Counted daily.",))  # type: ignore[arg-type]
    ui.SectionPart("Hosts", head=True)  # type: ignore[arg-type]
    ui.SectionPart("Hosts", moment="2026-11-01")  # type: ignore[arg-type]
    ui.SectionPart("Hosts", count=1.5)  # type: ignore[arg-type]
    ui.SectionPart("Hosts", template=("example/_hosts.html",))  # type: ignore[arg-type]


def an_allocation(cash: Decimal, owed: Decimal) -> None:
    # Parts of one whole, each its own amount; the host draws the bar.
    bar = ui.AllocationBar("Assets by kind", (ui.Share("Cash", float(cash), 1), ui.Share("Owed", float(owed), 2)))
    assert_type(bar.parts, tuple[ui.Share, ...])
    assert_type(bar.spoken, str)
    ui.AllocationBar("Assets by kind", [ui.Share("Cash", 1.0, 1)])  # type: ignore[arg-type]
    ui.Share("Cash", 1.0, "one")  # type: ignore[arg-type]
    # A part its legend links onward, with its amount as the page says it.
    owing = ui.Share("Owed", float(owed), 2, url="/example/?type=owed", detail=f"-${owed}", tone="attention")
    assert_type(owing.url, str)
    assert_type(owing.detail, str)
    assert_type(owing.tone, str)
    assert_type(bar.legend, tuple[tuple[ui.Share, str], ...])
    ui.Share("Cash", 1.0, 1, url=None)  # type: ignore[arg-type]


def a_list_with_its_own_fields(rows: Callable[..., dict[str, Any]]) -> None:
    # The fields each item of a list read keeps unless the caller names its own.
    spec = resources.ResourceSpec(
        "example.things",
        "Things",
        "Example things.",
        "example.read",
        rows,
        resources.ResourceQuery,
        list_fields=("name",),
    )
    assert_type(spec.list_fields, tuple[str, ...])

    resources.ResourceSpec("example.things", "Things", "Example things.", "example.read", list_fields="name")  # type: ignore[arg-type]


def a_list_with_the_deployment_ceiling(rows: Callable[..., dict[str, Any]]) -> None:
    # A list query that inherits its first ``limit`` takes the deployment's ceiling with it.
    class Query(resources.TopQuery):
        name: str | None = None

    assert_type(Query(limit=10).limit, int)
    resources.ResourceSpec("example.things", "Things", "Example things.", "example.read", rows, Query)

    resources.TopQuery(limit="many")  # type: ignore[arg-type]


def a_list_narrowed_by_status_and_text(rows: Callable[..., dict[str, Any]]) -> None:
    # A list's status and text filters say what every list's do; each may be left out.
    class Query(resources.TopQuery):
        status: resources.StatusFilter = None
        query: resources.TextFilter = None

    narrowed = Query(status="open", query="example")
    assert_type(narrowed.status, str | None)
    assert_type(narrowed.query, str | None)
    resources.ResourceSpec("example.things", "Things", "Example things.", "example.read", rows, Query)

    Query(status=1)  # type: ignore[arg-type]
    Query(query=["example"])  # type: ignore[arg-type]


def a_capability_renamed() -> None:
    # A plugin's migration moves the rows the host keeps under its old capability names.
    renamed = migrations.RenameCapabilities({"example.entries": "example.entry.list"})
    assert_type(renamed, migrations.RenameCapabilities)
    assert_type(migrations.CAPABILITY_ROWS, tuple[str, str])

    migrations.RenameCapabilities(["example.entries"])  # type: ignore[arg-type]
