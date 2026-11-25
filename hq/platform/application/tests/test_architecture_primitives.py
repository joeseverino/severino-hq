"""Questions asked in many places, each answered by one primitive or one definition.

The per-module ones are ast-grep rules: hostname-spelled-by-normalized-hostname,
command-linked-by-command-url, zone-membership-asked-by-in-zone,
entity-page-linked-by-entity-link and routes-reversed-through-their-owner.
"""

import re

from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag

from .source import template_files


class RouteOwnerTests(SimpleTestCase):
    """Routes are resolved in one place, so the answer can be remembered."""

    def test_a_remembered_route_is_djangos_and_follows_the_url_configuration(self):
        from django.urls import reverse as django_reverse

        from hq.platform.application.routes import reverse

        for name, kwargs in (("dashboard", None), ("control_plane:detail", {"key": "example"})):
            self.assertEqual(reverse(name, kwargs=kwargs), django_reverse(name, kwargs=kwargs))
            self.assertEqual(reverse(name, kwargs=kwargs), django_reverse(name, kwargs=kwargs))
        with self.settings(FORCE_SCRIPT_NAME="/under"):
            from django.urls import set_script_prefix

            set_script_prefix("/under/")
            try:
                self.assertEqual(reverse("dashboard"), django_reverse("dashboard"))
            finally:
                set_script_prefix("/")
        self.assertEqual(reverse("dashboard"), django_reverse("dashboard"))


@tag("source", "tree")
class FragmentPrimitiveTests(SimpleTestCase):
    """Reads are one component: `static/js/fragment.js` and the part it asks for.

    `lint:js` holds the scripts to it; this holds the templates that load
    and name its parts.
    """

    PRIMITIVE = "fragment.js"

    def test_the_primitive_loads_before_the_scripts_that_use_it(self):
        base = render_to_string("base.html")
        deferred = re.findall(r'js/([\w-]+\.js)"[^>]*\bdefer\b', base)

        self.assertGreater(len(deferred), 1)
        self.assertEqual(deferred[0], self.PRIMITIVE)

    def test_every_part_a_template_names_is_one_a_template_defines(self):
        sources = [path.read_text(encoding="utf-8") for _name, path in template_files()]
        named = {name for text in sources for name in re.findall(r'data-fragment-name="(\w+)"', text)}
        defined = {name for text in sources for name in re.findall(r"{% partialdef (\w+)", text)}

        self.assertTrue(named)
        self.assertEqual(named - defined, set())
