"""The queue's row, and a family folded under its name.

A row's meaning stands on its first line, wherever the queue is drawn;
several of one family are one row that opens to them
(``partials/_work_queue.html``).
"""

from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase

from hq.platform.application.action_items import by_source
from hq.platform.application.dashboard import finding_rows
from hq.platform.application.finding_model import Finding
from hq.platform.application.markup_testing import Element, all_elements


def _item(title: str, *, family: str = "", status: str = "attention") -> dict:
    (row,) = finding_rows(
        "example",
        "Example",
        (
            Finding(
                rule="example",
                subject=title,
                severity=status,
                title=title,
                explanation=f"What {title} means.",
                family=family,
            ),
        ),
    )
    return row


def _drawn(source: str, **context) -> list[Element]:
    request = RequestFactory().get("/")
    return all_elements(Template(source).render(Context({"request": request, **context})))


def families(items, **options):
    """``action_items.families``, looked up when called, so the module loads at a ref without it."""

    from hq.platform.application.action_items import families

    return families(items, **options)


def _rows(page: list[Element]) -> list[Element]:
    return [element for element in page if "data-attention-item" in element.attrs]


def _on_first_line(element: Element) -> bool:
    return any("row-line" in ancestor.classes for ancestor in element.ancestors)


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

    def test_a_family_of_one_rule_with_no_shared_fix_offers_nothing_on_its_row(self):
        def finding(title, url):
            return {**_item(title, family="Pins"), "key": f"example:finding:example-rule:{title}", "url": url}

        (entry,) = families([finding("a", "/example/a/"), finding("b", "/example/b/")])

        self.assertEqual(entry["lead"], [])

    def test_the_queue_page_folds_the_same_way(self):
        items = [_item("a", family="Pins"), _item("b"), _item("c", family="Pins")]

        (group,) = by_source(items)

        self.assertEqual(group["entries"], families(items))


class RowTests(SimpleTestCase):
    REST = '{% include "partials/_problems.html#rest" with rest=rest %}'

    def test_a_sections_problems_fold_a_family_into_one_row(self):
        page = _drawn(self.REST, rest=[_item("a", family="Pins"), _item("b", family="Pins"), _item("c")])

        (family,) = [element for element in page if "data-queue-family" in element.attrs]
        folded = [row for row in _rows(page) if row.attrs.get("data-folded")]
        self.assertEqual({row.attrs["data-folded"] for row in folded}, {family.attrs["id"]})
        self.assertEqual(len(folded), 2)
        self.assertEqual(len(_rows(page)), 3)

    def test_a_pages_problems_below_the_urgent_ones_are_under_a_heading(self):
        page = _drawn('{% include "partials/_problems.html" with problems=problems %}', problems=[_item("a")])

        headings = [element.words for element in page if element.tag in {"h2", "h3"}]
        self.assertTrue(any("Open problems" in words for words in headings))

    def test_a_rows_meaning_stands_on_its_first_line_wherever_the_queue_is_drawn(self):
        for source in (self.REST, '{% include "partials/_work_queue.html" with items=rest %}'):
            with self.subTest(source=source):
                page = _drawn(source, rest=[_item("a")])

                details = [element for element in page if "data-attention-detail" in element.attrs]
                self.assertTrue(details)
                self.assertTrue(all(_on_first_line(detail) for detail in details))


class FindingWordsTests(SimpleTestCase):
    """A row's why, clears and family are its rule's, and a finding no host
    rule raises (an extension's, a held request) says its own."""

    def said(self, rule: str) -> dict:
        (row,) = finding_rows(
            "example",
            "Example",
            (
                Finding(
                    rule=rule,
                    subject="example.com",
                    severity="attention",
                    title="Example reads late",
                    explanation="Example.",
                    why="The example was last read a week ago.",
                    clears="the example is read again",
                    family="Late examples",
                ),
            ),
        )
        return {field: row[field] for field in ("why", "clears", "family")}

    def test_a_finding_no_rule_raises_is_said_in_its_own_words(self):
        self.assertEqual(
            self.said("example-unruled"),
            {
                "why": "The example was last read a week ago.",
                "clears": "the example is read again",
                "family": "Late examples",
            },
        )

    def test_a_host_rules_finding_is_said_in_its_rules_words(self):
        from hq.platform.application.findings import finding_rules

        rule = finding_rules()[0]

        said = self.said(rule.name)

        self.assertEqual((said["clears"], said["family"]), (rule.clears, rule.title))
        self.assertNotEqual(said["why"], "The example was last read a week ago.")

    def test_a_held_request_is_a_held_row(self):
        (row,) = finding_rows(
            "",
            "Approvals",
            (
                Finding(
                    rule="approval",
                    subject="1",
                    severity="attention",
                    title="Example",
                    explanation="Example.",
                    held=True,
                ),
            ),
        )

        self.assertTrue(row["held"])

    def test_a_row_with_no_page_to_open_leads_with_its_first_press(self):
        from hq.platform.application.finding_steps import Press

        from .links import example_command

        fix = example_command("fix", "Fix it", "remote_write", "/example/fix/", method="POST")
        (row,) = finding_rows(
            "example",
            "Example",
            (
                Finding(
                    rule="example",
                    subject="",
                    severity="attention",
                    title="Example",
                    explanation="Example.",
                    steps=(Press(fix),),
                ),
            ),
        )

        self.assertEqual(row["action"], fix.label)
