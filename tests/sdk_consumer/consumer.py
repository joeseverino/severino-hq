"""An extension's use of ``hq_sdk``, as mypy reads it. Never imported.

The test that every export is annotated is satisfied by ``Any``, which checks
nothing. This holds the annotations to what they promise, from the side an
extension sees: each valid call pins what it returns with ``assert_type``, so
a return that slides to ``Any`` fails, and each call an extension must not be
able to make carries the error mypy reports for it. ``warn_unused_ignores``
fails the gate the day one of those stops being an error.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, assert_type, override

from django.db import models
from django.urls import reverse_lazy

from hq_sdk import (
    capabilities,
    demo,
    jobs,
    money,
    outbound,
    pages,
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
    assert_type(validation.sentence_closes("src"), list[str])


def the_demo(due: date, at: datetime) -> None:
    # A substitution returns what it was given, moved or not.
    assert_type(demo.day(due), date)
    assert_type(demo.day(at), datetime)
    assert_type(demo.day(None), None)
    assert_type(demo.amount(Decimal(1), key="example"), Decimal)
    assert_type(demo.label("Example", key="example"), str)
    assert_type(demo.showing_demo(), bool)

    demo.amount(Decimal(1))  # type: ignore[call-arg]


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
    assert_type(outbound.run_now("example.work", principal=principal), jobs.Job)
    assert_type(
        capabilities.execute_capability("example.do", {"name": "x"}, principal=principal),
        dict[str, Any],
    )
    assert_type(resources.list_resource("example.things", principal=principal), dict[str, Any])
    assert_type(resources.get_resource("example.things", 1, principal=principal), dict[str, Any])
    assert_type(
        resources.list_resource("example.things", principal=principal, fields=["name", "parts.name"]),
        dict[str, Any],
    )
    assert_type(resources.get_resource("example.things", 1, principal=principal, fields=("name",)), dict[str, Any])

    # Every one of these acts as someone, and says who.
    outbound.run_now("example.work")  # type: ignore[call-arg]
    capabilities.execute_capability("example.do", {})  # type: ignore[call-arg]
    resources.get_resource("example.things", 1)  # type: ignore[call-arg]
    resources.get_resource("example.things", 1, principal=principal, fields="name")  # type: ignore[arg-type]
    capabilities.execute_capability("example.do", ["x"], principal=principal)  # type: ignore[arg-type]


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

    capabilities.Describe()  # type: ignore[call-arg]
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


class AThing(pages.PageMixin):
    # A record's page: its list as the trail's first crumb, the record's own after it.
    page_trail = ("Things", reverse_lazy("example:things"))

    @override
    def trail_label(self, record: Any) -> str | None:
        return str(record)


class WronglyTrailed(pages.PageMixin):
    page_trail = "Things"  # type: ignore[assignment]

    @override
    def trail_label(self, record: Any) -> int:  # type: ignore[override]
        return 1


def a_trail(page: AThing) -> None:
    assert_type(page.get_page_trail(), tuple[tuple[str, str], ...])


class WronglyDeclared(tables.TableListMixin):
    table_empty = None  # type: ignore[assignment]
    table_columns = ("Name",)  # type: ignore[assignment]
    table_room = "wide"  # type: ignore[assignment]


def a_column() -> None:
    assert_type(tables.TableColumn("Cost", css="num-col").kind, str)
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


def announcements() -> None:
    assert_type(writes.ServiceWriteMixin.created_message, str)
    assert_type(writes.ServiceWriteMixin.updated_message, str)
    assert_type(writes.ServiceWriteMixin.deleted_message, str)
    assert_type(writes.ServiceWriteMixin.identity_result_key, str)
    assert_type(Renamed.created_message, str)
    assert_type(writes.WriteMessages.created_message, str)
    assert_type(WelcomedFirst.created_message, str)
    assert_type(WelcomedLast.created_message, str)
