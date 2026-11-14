"""The gallery draws every catalog entry on one page, so what an entry names
(an id a label, a heading or a control points at) is its own there."""

from collections import defaultdict

from django.template.context import RenderContext
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import all_elements
from hq.platform.core.tests.browser import catalog

# An id a page draws once by contract, which the gallery draws once for each
# entry drawing it, with why.
ONE_TO_A_PAGE = {
    "page-actions-more": "the menu of a page head's further actions, and a page draws one head",
}


class GalleryTests(SimpleTestCase):
    def test_no_two_entries_share_an_id(self):
        # What the gallery draws but its map, whose ids are its families'.
        drawn_by = defaultdict(list)
        page = RenderContext()
        for name, entry in catalog.CATALOG.items():
            if entry.frame:
                continue
            for element in all_elements(entry.drawn(name, page)):
                if "id" in element.attrs and not element.inert:
                    drawn_by[element.attrs["id"]].append(name)
        shared = {id_: names for id_, names in drawn_by.items() if len(names) > 1}

        self.assertEqual({id_: names for id_, names in shared.items() if id_ not in ONE_TO_A_PAGE}, {})
        # An id no two entries draw any longer needs no line.
        self.assertEqual(sorted(ONE_TO_A_PAGE), sorted(id_ for id_ in shared if id_ in ONE_TO_A_PAGE))
