"""A change's before and after is one part, whatever the change is of."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.approval_review import ChangePreview, compare
from hq.platform.application.payload_words import ChangeRow
from hq.platform.application.tests.markup import all_elements, elements, select, with_class, within
from hq.platform.application.ui import MISSING


def _drawn(preview: ChangePreview) -> str:
    return Template("{% change_table preview %}").render(Context({"preview": preview}))


def _headings(html: str) -> list[str]:
    return [
        heading.words for heading in elements(html, "th") if any(around.tag == "thead" for around in heading.ancestors)
    ]


def _sides(html: str, folds: str) -> dict[str, list[str]]:
    """The words of each side's cells in the rows folded under ``folds``, by their ``data-side``."""

    cells = [cell for cell in within(html, "data-folded", folds) if cell.tag == "td"]
    return {
        side: [cell.words.strip() for cell in cells if cell.attrs.get("data-side") == side]
        for side in ("before", "after")
    }


class ChangeTableTests(SimpleTestCase):
    def test_a_change_with_nothing_in_it_draws_nothing(self):
        self.assertEqual(_drawn(compare({}, {}, label="Would change")).strip(), "")

    def test_a_value_that_moves_is_read_beside_what_it_was(self):
        html = _drawn(compare({"example_name": "before"}, {"example_name": "after"}, label="Would change"))

        self.assertEqual(_headings(html), ["Field", "Before", "After", "Status"])
        self.assertTrue(with_class(html, "is-compare"), "the two values share the row evenly")
        self.assertEqual([cell.words for cell in select(html, "data-side")], ["before", "after"])

    def test_what_is_written_where_nothing_stood_is_one_column(self):
        html = _drawn(compare({}, {"example_name": "after"}, label="Would be applied"))

        self.assertNotIn("Before", _headings(html))
        self.assertEqual([cell.words for cell in select(html, "data-side")], ["after"])

    def test_a_redacted_value_is_said_to_have_changed_and_never_shown(self):
        row = ChangeRow(label="Example token", before="", after="", change="changed", redacted=True)
        html = _drawn(ChangePreview(label="Changes", rows=(row,), lines=()))

        self.assertEqual([cell.words for cell in select(html, "data-side")], ["redacted", "redacted"])

    def test_a_record_that_moves_is_one_row_its_fields_folded_under_it_and_one_that_stays_is_only_named(self):
        from hq.platform.application.approval_review import grouped

        records = {"example.first": {"example_effect": "read"}, "example.second": {"example_effect": "read"}}
        moved = {**records, "example.first": {"example_effect": "change", "example_mode": "fast"}}
        html = _drawn(grouped({"records": records}, {"records": moved}, label="Would change"))

        # One parse, so each cell's row is the same object as the row.
        shown = all_elements(html)
        (head,) = [row for row in shown if "data-folds" in row.attrs]
        in_head = [element for element in shown if any(row is head for row in element.ancestors)]
        (name,) = [cell for cell in in_head if cell.tag == "th"]
        self.assertEqual(name.words.strip(), "example.first", "the row is named by its record alone")
        self.assertNotIn("›", html, "no row is named by its record and its field")
        (status,) = [pill for pill in in_head if "pill" in pill.classes]
        self.assertEqual(status.words, "Changed")
        folds = head.attrs["id"]
        fields = [cell.words for cell in within(html, "data-folded", folds) if cell.tag == "th"]
        self.assertEqual(fields, ["Example effect", "Example mode"])
        self.assertEqual(_sides(html, folds), {"before": ["read", MISSING], "after": ["change", "fast"]})
        said = [pill.words for pill in within(html, "data-folded", folds) if "pill" in pill.classes]
        self.assertEqual(said, ["Changed", "New"], "each field of a record that moves says its own change")
        self.assertFalse(with_class(html, "row-group"), "no row only says what the rows under it say")
        (foot,) = with_class(html, "section-foot")
        self.assertIn("example.second", foot.words)
        (count,) = [element for element in with_class(html, "count") if element.words.strip().isdigit()]
        self.assertEqual(count.words.strip(), "1", "a record that moves counts as one")

    def test_a_record_the_change_widens_has_its_rows_marked_and_no_sentence_of_its_own(self):
        from dataclasses import replace

        from hq.platform.application.approval_review import grouped

        records = {"example.first": {"example_effect": "read"}}
        shown = grouped({"records": records}, {"records": {"example.first": {"example_effect": "change"}}}, label="X")
        html = _drawn(replace(shown, groups=tuple(replace(group, widens=True) for group in shown.groups)))

        (row,) = select(html, "data-attention")
        self.assertEqual(row.tag, "tr")
        self.assertIn("example.first", row.words)

    def test_a_change_keeps_its_own_order(self):
        rows = {f"example_{name}": name for name in ("zulu", "alpha", "mike")}
        html = _drawn(compare({}, rows, label="Would be applied"))

        self.assertTrue(_headings(html))
        self.assertFalse(select(html, "data-sort-local"), "no heading offers to sort a change")
        self.assertFalse(select(html, "aria-sort"))

    def test_a_record_that_comes_whole_is_one_row_its_name_and_its_summary_its_fields_folded_under_it(self):
        from hq.platform.application.approval_review import grouped

        # approval_review.SUMMARY, written out so the test reads at a ref without it.
        SUMMARY = "_summary"
        summary = {"label": "An example record", "value": "Changes an example thing"}
        before = {"commit": "example-old", "records": {"example.first": {"example_effect": "read"}}}
        after = {
            "commit": "example-new",
            "records": {
                **before["records"],
                "example.second": {SUMMARY: summary, "example_effect": "change", "example_mode": "fast"},
            },
        }
        html = _drawn(grouped(before, after, label="Would change"))

        (head,) = select(html, "data-folds")
        self.assertIn(summary["label"], head.words)
        cells = [cell for cell in select(html, "data-side") if any("data-folds" in row.attrs for row in cell.ancestors)]
        self.assertEqual(len(cells), 2)
        self.assertEqual(cells[0].words, MISSING, "nothing stood before it")
        self.assertEqual(cells[1].words, summary["value"], "what it is, after")
        self.assertNotIn("example.second", html, "a record with a name is named by it, never by its key")
        self.assertEqual([row.words.count(summary["value"]) for row in elements(html, "tr")].count(1), 1)
        self.assertFalse(with_class(html, "row-group"), "the record is said once, on its own row")
        folds = head.attrs["id"]
        self.assertEqual(len(select(html, "data-folded", folds)), 2)
        self.assertEqual(_sides(html, folds), {"before": ["", ""], "after": ["change", "fast"]})
        self.assertNotIn(SUMMARY, html)
        # The count is what a reader sees move: the commit, and the new record as one.
        (count,) = [element for element in with_class(html, "count") if element.words.strip().isdigit()]
        self.assertEqual(count.words.strip(), "2")

    def test_a_record_that_goes_whole_has_its_fields_under_before_and_nothing_after(self):
        from hq.platform.application.approval_review import grouped

        records = {"example.first": {"example_effect": "read"}, "example.second": {"example_mode": "fast"}}
        html = _drawn(
            grouped({"records": records}, {"records": {"example.first": records["example.first"]}}, label="X")
        )

        (head,) = select(html, "data-folds")
        self.assertEqual(_sides(html, head.attrs["id"]), {"before": ["fast"], "after": [""]})

    def test_a_document_that_will_not_parse_is_a_line_diff(self):
        notes = ("# An example\nfirst", "# An example\nsecond")
        html = _drawn(compare({"example_notes": notes[0]}, {"example_notes": notes[1]}, label="Would change"))

        (code,) = elements(html, "pre")
        self.assertIn("+second", code.words)


class ChangeKindTests(SimpleTestCase):
    def test_each_row_says_in_its_status_column_whether_it_is_new_changed_or_removed_each_in_its_own_tone(self):
        from hq.platform.application.approval_review import grouped

        before = {"example.kept": {"example_effect": "read"}, "example.gone": {"example_effect": "read"}}
        after = {"example.kept": {"example_effect": "change"}, "example.fresh": {"example_effect": "read"}}
        html = _drawn(grouped({"records": before}, {"records": after}, label="Would change"))

        self.assertEqual(_headings(html)[-1], "Status")
        # One parse, so each cell's row and each pill's cell are the same objects.
        shown = all_elements(html)
        last = {}
        for cell in (element for element in shown if element.tag == "td"):
            row = [around for around in cell.ancestors if around.tag == "tr"][-1]
            # A row's open line repeats its Status for a box too narrow to show the column.
            if "row-detail" not in row.classes:
                last[id(row)] = (row, cell)
        said, tones = {}, {}
        for row, status in last.values():
            if "data-folded" in row.attrs:
                if "kept" not in row.attrs["data-folded"]:
                    self.assertEqual(status.words, "", "a record that comes or goes whole says its change")
                continue
            (name,) = [name for name in ("example.kept", "example.gone", "example.fresh") if name in row.words]
            (pill,) = [el for el in shown if "pill" in el.classes and any(a is status for a in el.ancestors)]
            said[name], tones[name] = pill.words, pill.classes
        self.assertEqual(said, {"example.kept": "Changed", "example.gone": "Removed", "example.fresh": "New"})
        self.assertEqual(len(set(tones.values())), 3, "each kind of change is drawn in a tone of its own")
