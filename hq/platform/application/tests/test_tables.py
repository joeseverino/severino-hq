"""The list contract's wording: a sort names its order the way a person would,
and a row is named by what it is."""

import re

from django.db import models
from django.test import SimpleTestCase
from django.urls import get_resolver

from ..tables import TableColumn, TableListMixin

# A sort is "A–Z", "Oldest", "Lowest": never the direction of the query.
QUERY_WORDS = re.compile(r"\b(reverse|ascending|descending)\b", re.IGNORECASE)


def _list_views() -> list[type]:
    _ = get_resolver().url_patterns  # every view module is imported once the URLs are
    found, waiting = [], list(TableListMixin.__subclasses__())
    while waiting:
        view = waiting.pop()
        found.append(view)
        waiting.extend(view.__subclasses__())
    return found


class SortLabelTests(SimpleTestCase):
    def test_a_column_of_words_sorts_a_to_z(self):
        self.assertEqual([sort.label for sort in TableColumn("Status", "status").sorts()], ["Status A–Z", "Status Z–A"])

    def test_no_list_names_a_sort_by_its_query(self):
        views = _list_views()
        self.assertGreater(len(views), 5, "The walk found almost no list views")
        for view in views:
            columns = view.table_columns if isinstance(view.table_columns, tuple) else ()
            labels = [sort.label for sort in view.table_sorts] + [
                sort.label for column in columns for sort in column.sorts()
            ]
            for label in labels:
                with self.subTest(view=view.__name__, label=label):
                    self.assertIsNone(QUERY_WORDS.search(label))


def _field(model: type[models.Model], path: str) -> models.Field | None:
    """The field a sort path ends on, across its relations."""

    field = None
    for name in path.lstrip("-").split("__"):
        field = model._meta.get_field(name)
        model = field.related_model or model
    return field


class RowNameTests(SimpleTestCase):
    def test_no_list_names_its_rows_by_a_date(self):
        """The first column names each row: on a phone it is what the row is
        read by. A date says when, so it never stands there; a list kept in
        order of time still sorts by it from a later column."""

        views = [view for view in _list_views() if isinstance(view.table_columns, tuple) and view.table_columns]
        self.assertGreater(len(views), 5, "The walk found almost no list views")
        for view in views:
            first = view.table_columns[0]
            model = getattr(view, "model", None)
            with self.subTest(view=view.__name__, column=first.label):
                self.assertNotIn("date-col", first.css.split(), "names its rows by a date")
                if first.sort and model is not None:
                    self.assertNotIsInstance(_field(model, first.sort), models.DateField, "names its rows by a date")
