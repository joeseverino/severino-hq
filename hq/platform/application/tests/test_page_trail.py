from types import SimpleNamespace

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import Resolver404, resolve

from ..pages import PageMixin
from ..routes import routed

ABOVE = (("Example", "/example/"), ("Things", "/example/things/"))


class Thing(PageMixin):
    page_trail = ABOVE

    def trail_label(self, record):
        return record.name


class DeclaredTrailTests(SimpleTestCase):
    """``page_trail`` is every crumb above the page, and a record's own follows them."""

    def test_a_page_without_a_record_is_its_declared_crumbs(self):
        self.assertEqual(Thing().get_page_trail(), ABOVE)

    def test_a_record_named_by_trail_label_ends_the_trail(self):
        page = Thing()
        page.object = SimpleNamespace(name="One", get_absolute_url=lambda: "/example/things/one/")
        self.assertEqual(page.get_page_trail(), (*ABOVE, ("One", "/example/things/one/")))

    def test_a_record_trail_label_leaves_unnamed_adds_nothing(self):
        page = Thing()
        page.object = SimpleNamespace(name=None, get_absolute_url=lambda: "/example/things/one/")
        self.assertEqual(page.get_page_trail(), ABOVE)

    def test_a_page_that_declares_no_trail_has_none_even_for_a_record(self):
        class Bare(PageMixin):
            def trail_label(self, record):
                return "One"

        page = Bare()
        page.object = SimpleNamespace(get_absolute_url=lambda: "/example/one/")
        self.assertEqual(page.get_page_trail(), ())

    def test_one_crumb_written_bare_is_refused_where_the_view_is_declared(self):
        for wrong in (("Things", "/example/things/"), "Things", (("Things",),), [("Things", "/example/things/")]):
            with self.subTest(wrong=wrong), self.assertRaisesMessage(ImproperlyConfigured, "Wrong.page_trail"):
                type("Wrong", (PageMixin,), {"page_trail": wrong})

    def test_every_routed_page_trail_links_to_a_routed_page(self):
        declared = {
            route.view: route.view.page_trail
            for route in routed()
            if isinstance(route.view, type) and issubclass(route.view, PageMixin) and route.view.page_trail
        }
        self.assertTrue(declared, "no routed page declares a trail, so this reads nothing")
        for view, trail in declared.items():
            for label, url in trail:
                with self.subTest(view=view.__qualname__, crumb=label):
                    try:
                        resolve(str(url))
                    except Resolver404:
                        self.fail(f"{url} routes nowhere")
