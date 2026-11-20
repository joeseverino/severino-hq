"""Every row of a list is reached by following each page's ``next``, in
every order the list declares, and ``previous`` reads the page before."""

from contextlib import suppress
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.db import transaction
from django.test import TestCase

from hq.domains.contacts.inbox import Inbox
from hq.domains.control_plane.models import ManagedResource
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..projection import MAX_PAGE_SIZE
from ..read_models import documentation_status
from ..resources import list_resource, resource_registry
from ..security import Principal, operator_capabilities

# The query field a page past the first is asked by.
PAGE_FIELD = "page"
READER = Principal("example-reader", "test", operator_capabilities())
ROWS = MAX_PAGE_SIZE + 37


def walked(name, query, *, limit=MAX_PAGE_SIZE):
    """Every page of ``name`` under ``query``, by following each page's next."""

    pages = [list_resource(name, {**query, "limit": limit}, principal=READER, fields=["*"])]
    while pages[-1]["next"] is not None:
        pages.append(
            list_resource(
                name, {**query, "limit": limit, PAGE_FIELD: pages[-1]["next"]}, principal=READER, fields=["*"]
            )
        )
        if len(pages) > ROWS:
            raise AssertionError("the walk never ended")
    return pages


class ListPagingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Project.objects.bulk_create(Project(name=f"Example {n:03d}", slug=f"example-{n:03d}") for n in range(ROWS))
        day = date(2026, 1, 1)
        # Two expenses on each day, so the order's ties are broken by the key beneath it.
        Expense.objects.bulk_create(
            Expense(date=day + timedelta(days=n // 2), vendor="Example", item=f"Item {n}", total_cost=Decimal(1))
            for n in range(ROWS)
        )
        ManagedResource.objects.bulk_create(
            ManagedResource(key=f"example-{n:03d}", kind="adguard.rewrite", spec={}) for n in range(ROWS)
        )

    def test_a_list_longer_than_a_page_is_walked_to_its_end(self):
        for name, key in (("projects", "slug"), ("expenses", "id"), ("infrastructure", "key")):
            with self.subTest(name=name):
                pages = walked(name, {})
                seen = [item[key] for page in pages for item in page["items"]]

                self.assertGreater(len(pages), 1)
                self.assertEqual(len(seen), ROWS)
                self.assertEqual(len(set(seen)), ROWS, "a row shown twice")
                self.assertTrue(all(page["truncated"] for page in pages[:-1]))
                self.assertFalse(pages[-1]["truncated"])
                self.assertEqual({page["total"] for page in pages}, {ROWS})

    def test_a_derived_list_is_walked_to_its_end(self):
        pages = walked("readings", {}, limit=7)
        whole = list_resource("readings", {"limit": MAX_PAGE_SIZE}, principal=READER)

        self.assertGreater(len(pages), 1)
        self.assertEqual(
            [item["kind"] for page in pages for item in page["items"]], [item["kind"] for item in whole["items"]]
        )


def _comparable(value):
    """A value as its column orders it: nothing first, an amount as a number."""

    if isinstance(value, str):
        with suppress(ArithmeticError):
            value = Decimal(value)
    return (value is not None, value)


class EveryPageNamesTheOnesBesideItTests(TestCase):
    """A kind whose query takes a page names the next on every full page with
    more behind it, and the page after it names the way back."""

    @classmethod
    def setUpTestData(cls):
        # The example estate, so most kinds hold more than one row.
        with throwaway_media(), seeds.built("standard"):
            pass

    def test_every_paged_kind_names_the_page_past_a_full_one(self):
        paged = {
            name
            for name, spec in resource_registry().items()
            if spec.listing and PAGE_FIELD in spec.listing.query.model_fields
        }
        rows = tuple({"id": n, "name": f"Example {n}", "email": "a@example.com", "status": "new"} for n in range(3))
        self.assertTrue(paged)
        walked_past = set()
        with mock.patch("hq.domains.contacts.inbox.current", return_value=Inbox("ok", rows=rows)):
            for name in sorted(paged):
                with self.subTest(name=name):
                    first = list_resource(name, {"limit": 1}, principal=READER, fields=["*"])
                    more = list_resource(name, {"limit": MAX_PAGE_SIZE}, principal=READER)["count"] > 1
                    self.assertEqual(first["truncated"], more)
                    if not more:
                        self.assertIsNone(first["next"])
                        continue
                    self.assertIsNotNone(first["next"], "a full page with more behind it names no next")
                    second = list_resource(
                        name, {"limit": 1, PAGE_FIELD: first["next"]}, principal=READER, fields=["*"]
                    )
                    self.assertNotEqual(second["items"], first["items"])
                    self.assertIsNone(first["previous"])
                    back = list_resource(
                        name, {"limit": 1, PAGE_FIELD: second["previous"]}, principal=READER, fields=["*"]
                    )
                    self.assertEqual(back["items"], first["items"])
                    walked_past.add(name)
        # The kinds that cut a list derived whole, among those the estate fills.
        self.assertLessEqual({"calendar", "contact.submissions", "readings"}, walked_past)

    def test_every_declared_sort_pages_in_its_order(self):
        reads = {name: spec.listing for name, spec in resource_registry().items() if spec.listing}
        self.assertTrue(any(read.sorts for read in reads.values()))
        for name, read in reads.items():
            for sort in read.sorts:
                with self.subTest(name=name, sort=sort.name):
                    asked = {"sort": sort.name}
                    whole = list_resource(name, {**asked, "limit": MAX_PAGE_SIZE}, principal=READER, fields=["*"])
                    self.assertGreater(whole["count"], 1, "the estate holds too few rows to show an order")
                    # An order by one field its items carry is read off them.
                    key = sort.name.removeprefix("-")
                    if sort.ordering == (sort.name,) and key in read.fields:
                        held = [_comparable(item[key]) for item in whole["items"]]
                        self.assertEqual(held, sorted(held, reverse=sort.name.startswith("-")))
                    first = list_resource(name, {**asked, "limit": 1}, principal=READER, fields=["*"])
                    second = list_resource(
                        name, {**asked, "limit": 1, PAGE_FIELD: first["next"]}, principal=READER, fields=["*"]
                    )
                    self.assertEqual(first["items"] + second["items"], whole["items"][:2])
                    back = list_resource(
                        name, {**asked, "limit": 1, PAGE_FIELD: second["previous"]}, principal=READER, fields=["*"]
                    )
                    self.assertEqual(back["items"], first["items"])

    def test_every_declared_filter_and_toggle_keeps_only_what_it_names(self):
        narrowed = {
            name: spec
            for name, spec in resource_registry().items()
            if spec.listing and (spec.listing.filters or spec.listing.toggles)
        }
        self.assertTrue(narrowed)
        for name, spec in narrowed.items():
            read, key = spec.listing, spec.identifier
            whole = {item[key] for page in walked(name, {}) for item in page["items"]}
            for each in read.filters:
                chosen = [value for value, _ in each.options()[:2]]
                with self.subTest(name=name, filter=each.name):
                    kept = [item for page in walked(name, {each.name: ",".join(chosen)}) for item in page["items"]]
                    # A filter named for a field keeps only the records holding a value asked.
                    if each.name in read.fields:
                        self.assertLessEqual({str(item[each.name]) for item in kept}, {*chosen})
                    # One that keeps what the whole list leaves out reads past it.
                    if each.unasked is None:
                        self.assertLessEqual({item[key] for item in kept}, whole)
            # A toggle keeps the records its example says, and none it leaves out.
            for toggle in read.toggles:
                with self.subTest(name=name, toggle=toggle.name), transaction.atomic():
                    self.assertIsNotNone(toggle.example, "a toggle carries the example that shows what it keeps")
                    kept, left = toggle.example()
                    found = {item[key] for page in walked(name, {toggle.name: True}) for item in page["items"]}
                    self.assertLessEqual({*kept}, found)
                    self.assertFalse({*left} & found)
                    transaction.set_rollback(True)

    def test_every_sort_example_reads_in_its_order(self):
        for name, spec in resource_registry().items():
            for sort in spec.listing.sorts if spec.listing else ():
                if sort.example is None:
                    continue
                with self.subTest(name=name, sort=sort.name), transaction.atomic():
                    made = list(sort.example())
                    found = [
                        item[spec.identifier] for page in walked(name, {"sort": sort.name}) for item in page["items"]
                    ]
                    self.assertEqual([found_id for found_id in found if found_id in made], made)
                    transaction.set_rollback(True)


class DocumentationStatusPagingTests(TestCase):
    def test_the_summary_holds_one_page_and_its_next_reads_the_rest(self):
        DocumentationRecord.objects.bulk_create(
            DocumentationRecord(doc_id=f"rb-example-{n:03d}", title=f"Example {n}") for n in range(ROWS)
        )

        status = documentation_status(principal=READER)
        rest = list_resource("documentation", {PAGE_FIELD: status["next"]}, principal=READER)

        seen = [record["doc_id"] for record in status["records"] + rest["items"]]
        self.assertEqual(status["total"], ROWS)
        self.assertLess(len(status["records"]), ROWS)
        self.assertGreater(len(seen), len(status["records"]))
        self.assertEqual(seen, [f"rb-example-{n:03d}" for n in range(len(seen))])
