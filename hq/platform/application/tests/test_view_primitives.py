"""The shared pieces a write or list view is built from: file fields, rows, totals and the write spine."""

from django.test import SimpleTestCase


class MultipleFileFieldTests(SimpleTestCase):
    """Several files selected must mean several files imported.

    Django's FileField binds one upload; pointed at a multiple input it keeps
    whichever the widget returned and drops the rest silently: an operator
    would see one file land and no explanation for the others.
    """

    @staticmethod
    def _upload(name, body=b"x"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, body)

    def test_every_selected_file_is_returned(self):
        from django.http import QueryDict
        from django.utils.datastructures import MultiValueDict

        from ..forms import MultipleFileField

        field = MultipleFileField(max_bytes=8, max_files=2)
        files = MultiValueDict({"f": [self._upload("a.csv"), self._upload("b.csv")]})
        data = field.widget.value_from_datadict(QueryDict(), files, "f")
        self.assertEqual([item.name for item in field.clean(data)], ["a.csv", "b.csv"])

    def test_widget_reads_the_full_list_from_the_request(self):
        from ..forms import MultipleFileField

        # The multiple attribute has to come from the widget: rendering it while
        # reading a single file is how uploads get dropped without an error.
        self.assertTrue(MultipleFileField(max_bytes=8, max_files=2).widget.attrs.get("multiple"))

    def test_each_file_is_validated_not_just_the_first(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from ..forms import MultipleFileField

        field = MultipleFileField(max_bytes=8, max_files=2, max_length=5)
        with self.assertRaises(FormValidationError):
            field.clean([self._upload("ok.csv"), self._upload("far-too-long.csv")])

    def test_no_selection_raises_the_ordinary_required_error(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from ..forms import MultipleFileField

        with self.assertRaises(FormValidationError):
            MultipleFileField(max_bytes=8, max_files=2).clean([])
        self.assertEqual(MultipleFileField(max_bytes=8, max_files=2, required=False).clean([]), [])

    def test_a_file_over_the_declared_size_or_count_is_refused_in_the_words_its_input_carries(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from ..forms import MultipleFileField

        field = MultipleFileField(max_bytes=8, max_files=2)
        attrs = field.widget.attrs
        self.assertEqual((attrs["data-max-bytes"], attrs["data-max-files"]), ("8", "2"))
        with self.assertRaises(FormValidationError) as refused:
            field.clean([self._upload("big.csv", b"x" * 9)])
        self.assertEqual(refused.exception.messages, [f"big.csv {attrs['data-too-large']}"])
        with self.assertRaises(FormValidationError) as refused:
            field.clean([self._upload("a.csv"), self._upload("b.csv"), self._upload("c.csv")])
        self.assertEqual(refused.exception.messages, [attrs["data-too-many"]])
        self.assertEqual(len(field.clean([self._upload("a.csv", b"x" * 8)])), 1)


class ListRowTests(SimpleTestCase):
    """State on a shared row is never carried by color alone."""

    def test_status_requires_its_own_text(self):
        from ..card_items import ListRow

        ListRow(title="Import", status="attention", badge="Needs review")
        with self.assertRaises(ValueError):
            ListRow(title="Import", status="attention")

    def test_status_must_come_from_the_one_vocabulary(self):
        from ..card_items import ListRow

        with self.assertRaises(ValueError):
            ListRow(title="Import", status="warning", badge="Warning")

    def test_a_row_named_by_its_link_draws_it_as_every_mention_is_drawn(self):
        from django.template.loader import render_to_string

        from hq.platform.application.markup_testing import select

        from ..card_items import ListRow
        from ..entity_links import EntityLink

        host = EntityLink("example-host", "/example/host/", kind="machine", kind_label="machine")
        drawn = render_to_string("partials/_record_list.html", {"rows": [ListRow(link=host, meta="An example note")]})

        (mention,) = select(drawn, "data-entity")
        self.assertEqual((mention.attrs.get("href"), mention.words), ("/example/host/", "example-host"))
        for named in ({"title": "Example host"}, {"url": "/example/other/"}):
            with self.subTest(named=named), self.assertRaises(ValueError):
                ListRow(link=host, **named)

    def test_a_moment_is_a_value_the_row_says_in_words(self):
        from django.template.loader import render_to_string
        from django.utils import timezone

        from hq.platform.application.markup_testing import text

        from ..card_items import ListRow
        from ..moments import when_day

        today = timezone.localdate()
        for stamp in ("2026-10-22 09:36:47", "2026-10-22T09:36:47Z", "2026-10-22", today):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                ListRow(title="Message", meta=stamp)
        row = ListRow(title="Message", meta="Draft", at=today)
        shown = text(render_to_string("partials/_record_list.html", {"rows": [row]}))
        self.assertIn(" ".join(f"Draft · {when_day(today)}".split()), shown)

    def test_a_snippet_quotes_the_records_text_with_its_match_marked(self):
        """A search hit's snippet is what the record holds, which a person or a
        provider wrote, so text HQ's own words refuse is drawn as written."""

        from django.template.loader import render_to_string

        from hq.platform.application.markup_testing import elements, text

        from ..card_items import ListRow

        quoted = "enabled: False at 2026-11-02 09:30, <b> "
        row = ListRow(title="Example host", snippet=((quoted, False), ("backup", True)))
        drawn = render_to_string("partials/_record_list.html", {"rows": [row]})

        self.assertEqual([mark.words for mark in elements(drawn, "mark")], ["backup"])
        self.assertIn("enabled: False at 2026-11-02 09:30, <b> backup", text(drawn))
        with self.assertRaises(ValueError):
            ListRow(title="Example host", detail=quoted)

    def test_a_places_chips_are_each_a_word_after_its_line(self):
        from django.template.loader import render_to_string

        from hq.platform.application.markup_testing import elements

        from ..card_items import ListRow

        row = ListRow(
            title="Example connection", url="/example/connection/", chips=("Reads accounts", "Reads balances")
        )
        drawn = render_to_string("partials/_record_list.html", {"rows": [row], "whole_rows": True})

        (link,) = elements(drawn, "a")
        self.assertEqual((link.attrs["href"], link.words), ("/example/connection/", "Example connection"))
        self.assertEqual([span.words for span in elements(drawn, "span") if span.words in row.chips], list(row.chips))

    def test_a_rows_actions_end_its_line_a_post_one_submitting_its_value(self):
        """A record moved or removed from its row posts through the page's one
        form, a glyph named aloud by its title, and one that cannot be pressed
        says why where it stands."""

        from django.template.loader import render_to_string

        from hq.platform.application.markup_testing import elements, select

        from ..card_items import ListRow
        from ..pages import PageAction

        row = ListRow(
            title="Example group",
            actions=(
                PageAction._built(
                    label="↑", url="/example/group/", method="post", value="up", title="Move up", disabled=True
                ),
                PageAction._built(label="↓", url="/example/group/", method="post", value="down", title="Move down"),
                PageAction("Open", "/example/group/open/"),
            ),
        )
        drawn = render_to_string("partials/_record_list.html", {"rows": [row]})

        (down,) = elements(drawn, "button")
        self.assertEqual(
            (down.attrs["formaction"], down.attrs["value"], down.attrs["aria-label"]),
            ("/example/group/", "down", "Move down"),
        )
        (held,) = select(drawn, "aria-disabled", "true")
        self.assertEqual(held.attrs["title"], "Move up")
        self.assertIn(("/example/group/open/", "Open"), [(a.attrs.get("href"), a.words) for a in elements(drawn, "a")])
