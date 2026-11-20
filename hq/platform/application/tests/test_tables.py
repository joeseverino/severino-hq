"""The list contract's wording: a sort names its order the way a person would,
and a row is named by what it is."""

import re

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.test import SimpleTestCase
from django.urls import get_resolver

from ..list_pages import ListPageMixin
from ..resources import resource_registry

# A sort is "A–Z", "Oldest", "Lowest": never the direction of the query.
QUERY_WORDS = re.compile(r"\b(reverse|ascending|descending)\b", re.IGNORECASE)


def _list_views() -> list[type]:
    _ = get_resolver().url_patterns  # every view module is imported once the URLs are
    found, waiting = [], [*ListPageMixin.__subclasses__()]
    while waiting:
        view = waiting.pop()
        found.append(view)
        waiting.extend(view.__subclasses__())
    return found


class SortLabelTests(SimpleTestCase):
    def test_no_list_names_a_sort_by_its_query(self):
        sorts = [sort for spec in resource_registry().values() if spec.listing for sort in spec.listing.sorts]
        self.assertGreater(len(sorts), 5, "The walk found almost no sorts")
        for label in [sort.label for sort in sorts]:
            with self.subTest(label=label):
                self.assertIsNone(QUERY_WORDS.search(label))


def _field(model: type[models.Model], path: str) -> models.Field | None:
    """The field a sort path ends on, across its relations, or None for an annotation."""

    field = None
    for name in path.lstrip("-").split("__"):
        try:
            field = model._meta.get_field(name)
        except FieldDoesNotExist:
            return None
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
            first, search = view.table_columns[0], resource_registry()[view.resource].search
            with self.subTest(view=view.__name__, column=first.label):
                self.assertNotIn("date-col", first.css.split(), "names its rows by a date")
                if first.sort and search is not None:
                    self.assertNotIsInstance(
                        _field(search.model, first.sort), models.DateField, "names its rows by a date"
                    )
