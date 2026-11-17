"""The markup reader reads a page as a browser draws it."""

from django.test import SimpleTestCase

from hq.platform.application.markup_testing import all_elements, links


class ShadowRootTests(SimpleTestCase):
    def test_a_declarative_shadow_root_is_read_as_the_page(self):
        html = (
            '<div id="host"><template shadowrootmode="open"><a href="/example/">Example</a></template></div>'
            '<template><a href="/example/cloned/">Cloned</a></template>'
        )

        self.assertEqual(links(html), [("Example", "/example/")])
        shadowed = {element.tag: getattr(element, "shadowed", None) for element in all_elements(html)}
        self.assertEqual(shadowed, {"div": False, "a": True})
