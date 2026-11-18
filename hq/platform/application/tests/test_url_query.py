"""The one way an address gets its query: added to the one it holds, before its fragment, every value kept."""

from django.http import QueryDict
from django.test import SimpleTestCase

from hq.platform.application.url_query import with_query


class WithQueryTests(SimpleTestCase):
    def test_a_query_joins_the_one_the_address_holds_before_its_fragment(self):
        self.assertEqual(with_query("/x/?a=1#row", {"next": "/y/?b=2"}), "/x/?a=1&next=%2Fy%2F%3Fb%3D2#row")

    def test_every_value_of_a_key_is_kept(self):
        self.assertEqual(with_query("/x/", {"kind": ("a", "b")}), "/x/?kind=a&kind=b")
        self.assertEqual(with_query("/x/", QueryDict("kind=a&kind=b")), "/x/?kind=a&kind=b")

    def test_nothing_to_add_leaves_the_address_and_a_fragment_given_replaces_its_own(self):
        self.assertEqual(with_query("/x/?a=1", {}), "/x/?a=1")
        self.assertEqual(with_query("/x/#old", None, fragment="map"), "/x/#map")
