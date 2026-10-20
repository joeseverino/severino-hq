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
from typing import Any, assert_type

from hq_sdk import capabilities, demo, jobs, money, outbound, pages, reads, resources, site, tables, testing


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

    money.quantize_money(1.5)  # type: ignore[arg-type]
    money.money("1.00")  # type: ignore[arg-type]


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
    assert_type(jobs.start("example.import", "Import", importing), jobs.Job)
    assert_type(jobs.reap(), int)
    assert_type(jobs.reap("example.import"), int)

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


def a_page() -> None:
    assert_type(pages.page_context("Example"), dict[str, pages.Page])
    assert_type(pages.PageAction("Open", "/example/", primary=True).css, str)

    pages.PageAction("Open")  # type: ignore[call-arg]
    pages.page_context("Example", actions=["Open"])  # type: ignore[list-item]
    assert_type(pages.page_context("Example", reads=("example.things",)), dict[str, pages.Page])
    pages.page_context("Example", reads=1)  # type: ignore[arg-type]


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
    table_columns = (tables.TableColumn("Name", "name"),)
    table_search_fields = ("name",)
    table_default_sort = "name"


class WronglyDeclared(tables.TableListMixin):
    table_empty = None  # type: ignore[assignment]
    table_columns = ("Name",)  # type: ignore[assignment]


def the_deployment() -> None:
    assert_type(site.host(), str)
    assert_type(testing.plain(b"2\xc2\xa0weeks"), str)
    assert_type(testing.plain("2 weeks"), str)

    testing.plain(2)  # type: ignore[arg-type]
