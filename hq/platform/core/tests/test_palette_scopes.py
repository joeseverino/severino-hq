"""The palette ranks its search groups by scope, and each scope it ranks is one a resource searches."""

from django.test import SimpleTestCase

from hq.platform.application.resources import resource_registry
from hq.platform.core.views import SearchView


class PaletteScopeTests(SimpleTestCase):
    def test_each_ranked_scope_is_one_a_resource_searches(self):
        # A rank keyed by a scope no resource searches ranks nothing, and its
        # group falls to the default without a word.
        searched = {spec.search.scope for spec in resource_registry().values() if spec.search}
        self.assertEqual(set(SearchView.palette_scope_priority) - searched, set())
