"""The list contract's wording: a sort names its order the way a person would."""

import re

from django.test import SimpleTestCase
from django.urls import get_resolver

from ..tables import TableColumn, TableListMixin

# A sort is "A–Z", "Oldest", "Lowest": never the direction of the query.
QUERY_WORDS = re.compile(r"\b(reverse|ascending|descending)\b", re.IGNORECASE)


def _list_views() -> list[type]:
    get_resolver().url_patterns  # every view module is imported once the URLs are
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
