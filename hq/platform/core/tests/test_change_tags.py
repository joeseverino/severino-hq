"""A change's before and after is one part, whatever the change is of."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.approval_review import ChangePreview, compare
from hq.platform.application.payload_words import ChangeRow
from hq.platform.application.tests.markup import elements, select, with_class, within


def _drawn(preview: ChangePreview) -> str:
    return Template("{% change_table preview %}").render(Context({"preview": preview}))


def _headings(html: str) -> list[str]:
    return [
        heading.words for heading in elements(html, "th") if any(around.tag == "thead" for around in heading.ancestors)
    ]


class ChangeTableTests(SimpleTestCase):
    def test_a_change_with_nothing_in_it_draws_nothing(self):
        self.assertEqual(_drawn(compare({}, {}, label="Would change")).strip(), "")

    def test_a_value_that_moves_is_read_beside_what_it_was(self):
        html = _drawn(compare({"example_name": "before"}, {"example_name": "after"}, label="Would change"))

        self.assertEqual(_headings(html), ["Field", "Before", "After"])
        self.assertTrue(with_class(html, "is-compare"), "the two values share the row evenly")
        self.assertEqual([cell.words for cell in elements(html, "td")], ["before", "after"])

    def test_what_is_written_where_nothing_stood_is_one_column(self):
        html = _drawn(compare({}, {"example_name": "after"}, label="Would be applied"))

        self.assertNotIn("Before", _headings(html))
        self.assertEqual([cell.words for cell in elements(html, "td")], ["after"])

    def test_a_redacted_value_is_said_to_have_changed_and_never_shown(self):
        row = ChangeRow(label="Example token", before="", after="", change="changed", redacted=True)
        html = _drawn(ChangePreview(label="Changes", rows=(row,), lines=()))

        self.assertEqual([cell.words for cell in elements(html, "td")], ["redacted", "redacted"])

    def test_each_record_that_changes_is_a_group_and_one_that_stays_is_only_named(self):
        from hq.platform.application.approval_review import grouped

        records = {"example.first": {"example_effect": "read"}, "example.second": {"example_effect": "read"}}
        moved = {**records, "example.first": {"example_effect": "change"}}
        html = _drawn(grouped({"records": records}, {"records": moved}, label="Would change"))

        groups = [group.words.split()[0] for group in with_class(html, "row-group")]
        self.assertEqual(groups, ["example.first"])
        self.assertFalse(select(html, "data-folds"), "nothing that changes is folded away")
        (foot,) = with_class(html, "section-foot")
        self.assertIn("example.second", foot.words)

    def test_a_change_keeps_its_own_order(self):
        rows = {f"example_{name}": name for name in ("zulu", "alpha", "mike")}
        html = _drawn(compare({}, rows, label="Would be applied"))

        self.assertTrue(_headings(html))
        self.assertFalse(select(html, "data-sort-local"), "no heading offers to sort a change")
        self.assertFalse(select(html, "aria-sort"))

    def test_a_record_that_comes_whole_is_headed_by_its_name_with_its_summary_its_one_row(self):
        from hq.platform.application.approval_review import grouped

        # approval_review.SUMMARY, written out so the test reads at a ref without it.
        SUMMARY = "_summary"
        summary = {"label": "An example record", "value": "Changes an example thing."}
        before = {"commit": "example-old", "records": {"example.first": {"example_effect": "read"}}}
        after = {
            "commit": "example-new",
            "records": {
                **before["records"],
                "example.second": {SUMMARY: summary, "example_effect": "change", "example_mode": "fast"},
            },
        }
        html = _drawn(grouped(before, after, label="Would change"))

        (header,) = [group for group in with_class(html, "row-group") if summary["label"] in group.words]
        self.assertIn("new", header.words)
        self.assertNotIn("example.second", html, "a record with a name is headed by it, never by its key")
        folds = header.attrs["id"]
        self.assertEqual(len(select(html, "data-folded", folds)), 2)
        cells = [cell for cell in within(html, "data-folded", folds) if cell.tag == "td"]
        self.assertEqual([cell.attrs.get("colspan") for cell in cells], ["2", "2"], "one value, no empty Before")
        shown = [row for row in elements(html, "tr") if summary["value"] in row.words]
        self.assertEqual(len(shown), 1)
        self.assertNotIn("data-folded", shown[0].attrs, "the summary stays in sight while its fields fold")
        self.assertNotIn(summary["label"], shown[0].words, "the summary is a sentence, not a field")
        self.assertNotIn(SUMMARY, html)
        # The count is what a reader sees move: the commit, and the new record as one.
        (count,) = [element for element in with_class(html, "count") if element.words.strip().isdigit()]
        self.assertEqual(count.words.strip(), "2")

    def test_a_document_that_will_not_parse_is_a_line_diff(self):
        notes = ("# An example\nfirst", "# An example\nsecond")
        html = _drawn(compare({"example_notes": notes[0]}, {"example_notes": notes[1]}, label="Would change"))

        (code,) = elements(html, "pre")
        self.assertIn("+second", code.words)
