"""The shared pieces a write or list view is built from: file fields, rows, totals and the write spine."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase


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

        field = MultipleFileField()
        files = MultiValueDict({"f": [self._upload("a.csv"), self._upload("b.csv")]})
        data = field.widget.value_from_datadict(QueryDict(), files, "f")
        self.assertEqual([item.name for item in field.clean(data)], ["a.csv", "b.csv"])

    def test_widget_reads_the_full_list_from_the_request(self):
        from ..forms import MultipleFileField

        # The multiple attribute has to come from the widget: rendering it while
        # reading a single file is how uploads get dropped without an error.
        self.assertTrue(MultipleFileField().widget.attrs.get("multiple"))

    def test_each_file_is_validated_not_just_the_first(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from ..forms import MultipleFileField

        field = MultipleFileField(max_length=5)
        with self.assertRaises(FormValidationError):
            field.clean([self._upload("ok.csv"), self._upload("far-too-long.csv")])

    def test_no_selection_raises_the_ordinary_required_error(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from ..forms import MultipleFileField

        with self.assertRaises(FormValidationError):
            MultipleFileField().clean([])
        self.assertEqual(MultipleFileField(required=False).clean([]), [])


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

        from ..card_items import ListRow
        from ..entity_links import EntityLink
        from .markup import select

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

        from ..card_items import ListRow
        from ..moments import when_day
        from .markup import text

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

        from ..card_items import ListRow
        from .markup import elements, text

        quoted = "enabled: False at 2026-11-02 09:30, <b> "
        row = ListRow(title="Example host", snippet=((quoted, False), ("backup", True)))
        drawn = render_to_string("partials/_record_list.html", {"rows": [row]})

        self.assertEqual([mark.words for mark in elements(drawn, "mark")], ["backup"])
        self.assertIn("enabled: False at 2026-11-02 09:30, <b> backup", text(drawn))
        with self.assertRaises(ValueError):
            ListRow(title="Example host", detail=quoted)

    def test_a_places_chips_are_each_a_word_after_its_line(self):
        from django.template.loader import render_to_string

        from ..card_items import ListRow
        from .markup import elements

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

        from ..card_items import ListRow
        from ..pages import PageAction
        from .markup import elements, select

        row = ListRow(
            title="Example group",
            actions=(
                PageAction("↑", "/example/group/", method="post", value="up", title="Move up", disabled=True),
                PageAction("↓", "/example/group/", method="post", value="down", title="Move down"),
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


class TableTotalsTests(TestCase):
    """Totals describe the filtered set, not the page you happen to be on."""

    def setUp(self):
        from decimal import Decimal

        from hq.domains.assets.models import Asset

        for index in range(5):
            Asset.objects.create(
                item_name=f"Item {index}",
                slug=f"item-{index}",
                total_cost=Decimal("100.00"),
                category="tools" if index < 2 else "office",
            )

    def _view(self, **attrs):
        from hq.domains.assets.models import Asset
        from hq.platform.application.tables import TableListMixin, TableTotal

        class View(TableListMixin):
            table_totals = (TableTotal("total_cost", "Total cost"),)

        view = View()
        for key, value in attrs.items():
            setattr(view, key, value)
        return view, Asset.objects.all()

    def test_it_sums_the_whole_queryset(self):
        from decimal import Decimal

        view, queryset = self._view()
        totals = view.table_totals_for(queryset)
        self.assertEqual(totals["total_cost"]["value"], Decimal("500.00"))
        self.assertEqual(totals["total_cost"]["label"], "Total cost")

    def test_it_follows_the_filter(self):
        from decimal import Decimal

        view, queryset = self._view()
        totals = view.table_totals_for(queryset.filter(category="tools"))
        self.assertEqual(totals["total_cost"]["value"], Decimal("200.00"))

    def test_a_view_declaring_no_totals_gets_none(self):
        from hq.platform.application.tables import TableListMixin

        class Bare(TableListMixin):
            pass

        from hq.domains.assets.models import Asset

        self.assertEqual(Bare().table_totals_for(Asset.objects.all()), {})

    def test_a_missing_queryset_is_not_an_error(self):
        view, _ = self._view()
        self.assertEqual(view.table_totals_for(None), {})


class ProjectedListTests(TestCase):
    """A list a projection fills: what its address asks reaches the projection
    as its parameters, and a page is cut in the database and read back whole."""

    def setUp(self):
        from hq.domains.jobs.models import Job

        for index in range(5):
            Job.objects.create(kind=f"example.work-{index}", label=f"Example {index}", state="succeeded")

    def _view(self, query=""):
        from django.test import RequestFactory
        from django.views.generic import ListView

        from hq.domains.jobs.models import Job
        from hq.platform.application.tables import (
            Projected,
            TableColumn,
            TableFilter,
            TableListMixin,
            TableSort,
        )

        asked = []

        class View(TableListMixin, ListView):
            paginate_by = 2
            table_columns = (TableColumn("Job", kind="identifier"),)
            table_filters = (TableFilter("kind", "Job", "never-read", ()),)
            table_sorts = (TableSort("newest", "Newest first", "never-read"), TableSort("oldest", "Oldest", ""))
            table_default_sort = "newest"

            def get_table_rows(self, query):
                asked.append(query)
                rows = Job.objects.order_by("kind" if query.sort == "oldest" else "-kind")
                if query.chosen("kind"):
                    rows = rows.filter(kind__in=query.chosen("kind"))
                return Projected(rows.values_list("kind", flat=True), lambda page: [f"read {kind}" for kind in page])

        view = View()
        view.setup(RequestFactory().get(f"/example/?{query}"))
        view.object_list = view.get_queryset()
        return view, asked

    def test_the_address_reaches_the_projection_as_its_parameters(self):
        _view, asked = self._view("kind=example.work-1&kind=example.work-2&q=+example+&sort=oldest")

        (query,) = asked
        self.assertEqual(query.chosen("kind"), ("example.work-1", "example.work-2"))
        self.assertEqual((query.search, query.sort, query.chosen("state")), ("example", "oldest", ()))

    def test_a_sort_the_list_does_not_declare_is_its_default(self):
        _view, asked = self._view("sort=-kind")

        self.assertEqual(asked[0].sort, "newest")

    def test_a_page_is_counted_cut_and_read_back(self):
        view, _asked = self._view("sort=oldest")

        context = view.get_context_data()

        self.assertEqual(context["paginator"].count, 5)
        self.assertEqual(list(context["object_list"]), ["read example.work-0", "read example.work-1"])
        self.assertEqual([column.kind for column in context["table"]["columns"]], ["identifier"])

    def test_a_list_of_the_model_s_queryset_is_as_it_was(self):
        from django.test import RequestFactory
        from django.views.generic import ListView

        from hq.domains.jobs.models import Job
        from hq.platform.application.tables import TableListMixin

        class View(TableListMixin, ListView):
            model = Job

        view = View()
        view.setup(RequestFactory().get("/example/"))

        self.assertEqual(view.get_queryset().count(), 5)


class WriteSpineContractTests(TestCase):
    """What a write view must declare, enforced where the fix is obvious.

    Both guards exist because the failure they replace is quiet. A service left
    as a plain function binds as a method and passes the view where the command
    belongs; a missing noun renders a success message with a hole in it.
    """

    def _view(self, **attributes):
        from ..writes import ServiceCreateMixin

        return type("AView", (ServiceCreateMixin,), attributes)

    def test_a_service_that_is_not_a_staticmethod_is_refused(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            self._view(service=lambda command, **kwargs: None)

        self.assertIn("staticmethod", str(caught.exception))

    def test_a_view_that_writes_must_say_what_it_writes(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            self._view(service=staticmethod(lambda command, **kwargs: None))

        self.assertIn("noun", str(caught.exception))
        self.assertIn("result_key", str(caught.exception))

    def test_a_complete_view_is_accepted(self):
        view = self._view(
            service=staticmethod(lambda command, **kwargs: None),
            noun="Thing",
            result_key="thing",
            identity_attr="slug",
        )

        self.assertEqual(view.noun, "Thing")

    def test_a_mixin_that_supplies_no_service_is_not_a_view(self):
        """Intermediate layers declare part of the contract and finish none of
        it, so completeness is asked of the class that supplies the service."""

        from ..writes import ServiceCreateMixin

        layer = type("PartialLayer", (ServiceCreateMixin,), {"noun": "Thing"})

        self.assertEqual(layer.noun, "Thing")

    def test_the_base_declares_no_noun_to_collide_with(self):
        """A placeholder value that is never correct still wins or loses by base
        order, so which one applies depends on how the bases were written."""

        from ..writes import ServiceWriteMixin

        for name in ("noun", "result_key", "identity_attr"):
            self.assertNotIn(name, vars(ServiceWriteMixin))
