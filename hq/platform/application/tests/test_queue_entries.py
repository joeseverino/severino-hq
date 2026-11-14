"""The queue's row in its two forms, and a family folded under its name.

A thing's problems drawn inside one of its sections are the row's dense form:
each one line, its meaning in the line beneath; several of one family are one
row that opens to them (``partials/_work_queue.html``).
"""

from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase

from hq.platform.application.action_items import by_source
from hq.platform.application.card_items import Insight
from hq.platform.application.dashboard import queue_item

from .markup import Element, all_elements


def _item(title: str, *, family: str = "", status: str = "attention") -> dict:
    return queue_item(
        "example",
        "Example",
        Insight(
            status=status,
            eyebrow="",
            title=title,
            value="",
            body=f"What {title} means.",
            key=f"example:{title}",
            family=family,
        ),
    )


def _drawn(source: str, **context) -> list[Element]:
    request = RequestFactory().get("/")
    return all_elements(Template(source).render(Context({"request": request, **context})))


def families(items, **options):
    """``action_items.families``, looked up when called, so the module loads at a ref without it."""

    from hq.platform.application.action_items import families

    return families(items, **options)


def _rows(page: list[Element]) -> list[Element]:
    return [element for element in page if "data-attention-item" in element.attrs]


def _below(element: Element) -> bool:
    return any("row-below" in ancestor.classes for ancestor in element.ancestors)


class FamiliesTests(SimpleTestCase):
    def test_a_family_of_several_is_one_entry_where_its_first_stands(self):
        items = [_item("a", family="Pins"), _item("b"), _item("c", family="Pins")]

        entries = families(items)

        self.assertEqual([entry["folded"] for entry in entries], [True, False])
        self.assertEqual([item["label"] for item in entries[0]["items"]], ["a", "c"])

    def test_an_item_of_no_family_and_a_family_of_one_stand_alone(self):
        entries = families([_item("a"), _item("b"), _item("c", family="Pins")])

        self.assertEqual([entry["folded"] for entry in entries], [False, False, False])

    def test_unfolded_every_item_stands_alone(self):
        entries = families([_item("a", family="Pins"), _item("b", family="Pins")], fold=False)

        self.assertEqual([len(entry["items"]) for entry in entries], [1, 1])

    def test_a_folded_family_says_its_worst_and_the_button_its_items_share(self):
        def led(item, url):
            return {**item, "action": "Open example", "url": url}

        same = [led(_item("a", family="Pins"), "/example/"), led(_item("b", family="Pins"), "/example/")]
        apart = [led(_item("a", family="Pins"), "/example/a/"), led(_item("b", family="Pins"), "/example/b/")]

        (shared,) = families(same)
        (split,) = families(apart)

        self.assertEqual(shared["summary"], "a, and 1 more")
        self.assertEqual([lead["url"] for lead in shared["lead"]], ["/example/"])
        self.assertEqual(split["lead"], [])

    def test_a_family_of_one_rule_with_no_shared_fix_opens_the_list_of_them(self):
        def finding(title, url):
            return {**_item(title, family="Pins"), "key": f"example:finding:example-rule:{title}", "url": url}

        (entry,) = families([finding("a", "/example/a/"), finding("b", "/example/b/")])

        (lead,) = entry["lead"]
        self.assertTrue(lead["url"].endswith("?rule=example-rule"))
        self.assertTrue(lead["recommended"])

    def test_the_queue_page_folds_the_same_way(self):
        items = [_item("a", family="Pins"), _item("b"), _item("c", family="Pins")]

        (group,) = by_source(items)

        self.assertEqual(group["entries"], families(items))


class DenseRowTests(SimpleTestCase):
    REST = '{% include "partials/_problems.html#rest" with rest=rest %}'

    def test_a_sections_problems_fold_a_family_into_one_row(self):
        page = _drawn(self.REST, rest=[_item("a", family="Pins"), _item("b", family="Pins"), _item("c")])

        (family,) = [element for element in page if "data-queue-family" in element.attrs]
        folded = [row for row in _rows(page) if row.attrs.get("data-folded")]
        self.assertEqual({row.attrs["data-folded"] for row in folded}, {family.attrs["id"]})
        self.assertEqual(len(folded), 2)
        self.assertEqual(len(_rows(page)), 3)

    def test_a_dense_rows_meaning_waits_in_the_line_beneath(self):
        page = _drawn(self.REST, rest=[_item("a")])

        details = [element for element in page if "data-attention-detail" in element.attrs]
        self.assertTrue(details)
        self.assertTrue(all(_below(detail) for detail in details))

    def test_a_pages_problems_below_the_urgent_ones_are_under_a_heading(self):
        page = _drawn('{% include "partials/_problems.html" with problems=problems %}', problems=[_item("a")])

        headings = [element.words for element in page if element.tag in {"h2", "h3"}]
        self.assertTrue(any("Open problems" in words for words in headings))

    def test_the_whole_queue_keeps_its_meaning_on_the_row(self):
        page = _drawn('{% include "partials/_work_queue.html" with items=items %}', items=[_item("a")])

        details = [element for element in page if "data-attention-detail" in element.attrs]
        self.assertTrue(details)
        self.assertFalse(any(_below(detail) for detail in details))
