"""Every row of a list is reached by following each page's ``next`` as it
came, and a cursor reads only the list and filters it was issued under."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

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
from ..resources import InvalidResourceInput, list_resource, resource_registry
from ..security import Capability, Principal

# The query field a page past the first is asked by.
PAGE_FIELD = "cursor"
READER = Principal("example-reader", "test", frozenset(Capability))
ROWS = MAX_PAGE_SIZE + 37


def walked(name, query, *, limit=MAX_PAGE_SIZE):
    """Every page of ``name`` under ``query``, by following each page's next."""

    pages = [list_resource(name, {**query, "limit": limit}, principal=READER, fields=["*"])]
    while pages[-1]["next"] is not None:
        pages.append(
            list_resource(name, {**query, "limit": limit, **pages[-1]["next"]}, principal=READER, fields=["*"])
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
        for name, key in (("projects", "slug"), ("expenses", "id"), ("infrastructure.resources", "key")):
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

    def test_a_cursor_under_other_filters_is_refused_naming_the_cursor(self):
        first = list_resource("projects", {"limit": 5}, principal=READER)

        with self.assertRaises(InvalidResourceInput) as refused:
            list_resource("projects", {"limit": 5, "query": "example-1", **first["next"]}, principal=READER)
        self.assertEqual(refused.exception.errors[0]["loc"], ["cursor"])

    def test_a_cursor_of_another_list_or_not_issued_by_hq_is_refused(self):
        cursor = list_resource("projects", {"limit": 5}, principal=READER)["next"]["cursor"]

        for name, sent in (
            ("infrastructure.resources", cursor),
            ("projects", cursor[:-2] + "xx"),
            ("projects", "example"),
        ):
            with self.subTest(name=name, sent=sent), self.assertRaises(InvalidResourceInput) as refused:
                list_resource(name, {"limit": 5, "cursor": sent}, principal=READER)
            self.assertEqual(refused.exception.errors[0]["loc"], ["cursor"])

    def test_the_last_page_offers_no_cursor(self):
        page = list_resource("projects", {"limit": MAX_PAGE_SIZE, "query": "example-00"}, principal=READER)

        self.assertEqual((page["count"], page["truncated"], page["next"]), (10, False, None))


class EveryCursorIsIssuedTests(TestCase):
    """A kind whose query offers a cursor issues one on every full page with more behind it."""

    @classmethod
    def setUpTestData(cls):
        # The example estate, so most kinds hold more than one row.
        with throwaway_media(), seeds.built("standard"):
            pass

    def test_every_paged_kind_issues_a_cursor_past_a_full_page(self):
        paged = {
            name
            for name, spec in resource_registry().items()
            if spec.list_query_type and PAGE_FIELD in spec.list_query_type.model_fields
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
                    self.assertIsNotNone(first["next"], "a full page with more behind it offers no cursor")
                    second = list_resource(name, {"limit": 1, **first["next"]}, principal=READER, fields=["*"])
                    self.assertNotEqual(second["items"], first["items"])
                    walked_past.add(name)
        # The kinds that cut a list derived whole, among those the estate fills.
        self.assertLessEqual({"calendar", "contact.submissions", "readings"}, walked_past)


class DocumentationStatusPagingTests(TestCase):
    def test_the_summary_holds_one_page_and_the_cursor_reads_the_rest(self):
        DocumentationRecord.objects.bulk_create(
            DocumentationRecord(doc_id=f"rb-example-{n:03d}", title=f"Example {n}") for n in range(ROWS)
        )

        status = documentation_status(principal=READER)
        rest = list_resource("documentation", dict(status["next"]), principal=READER)

        seen = [record["doc_id"] for record in status["records"] + rest["items"]]
        self.assertEqual(status["total"], ROWS)
        self.assertLess(len(status["records"]), ROWS)
        self.assertGreater(len(seen), len(status["records"]))
        self.assertEqual(seen, [f"rb-example-{n:03d}" for n in range(len(seen))])
