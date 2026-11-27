"""What every page and template says the one shared way: titles, counts, wording, buttons, trails and reads."""

from importlib import import_module
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag

from hq.platform.application.routes import routed

from .facts import scan
from .test_view_writes import routed_modules, shown


class CountedTests(SimpleTestCase):
    """One way to say how many, so a noun and its verb agree."""

    def test_a_phrase_without_its_plural_is_refused(self):
        from hq.platform.application.ui import counted

        with self.assertRaises(ValueError):
            counted(2, "needs output")


@tag("source", "tree")
class PageTrailTests(SimpleTestCase):
    """A view says its trail with ``page_trail`` and ``trail_label``, not a ``get_page_trail`` that restates them.

    The rule and its cases are ``page-trail-declared`` in tools/lint/ast-grep,
    which lint:ast-grep runs over the host; this runs it over every routed
    module, an installed extension's and the example plugin's included.
    """

    def test_no_view_overrides_get_page_trail_to_say_what_the_mixin_says(self):
        import yaml  # The image's suite collects this module without PyYAML.

        root = Path(__file__).resolve().parents[4]
        rule = yaml.safe_load((root / "tools/lint/ast-grep/rules/page-trail-declared.yml").read_text(encoding="utf-8"))
        restated = [
            f"{shown(root, found.path)}:{found.line}"
            for found in scan({rule["id"]: rule["rule"]}, *routed_modules(root))
        ]
        self.assertEqual(restated, [], "declare page_trail (and trail_label) on the view")


def _takes_post(view) -> bool:
    """Whether a routed class view takes a POST, as its route builds it. A
    function view's methods are its body's, which no declaration says."""

    owner = vars(view).get("view_class")
    if owner is None:
        return False
    built = owner(**getattr(view, "view_initkwargs", {}))
    return "post" in built.http_method_names and hasattr(built, "post")


class PageReadCapabilityTests(TestCase):
    """Every page the URL configuration routes reads under one capability, refused before its view runs.

    Walked from the URL configuration in force, function views and every
    installed extension's included, so a page added anywhere is held.
    A route that answers without a session (``login_not_required``)
    authorizes its own requests and is read as declared.
    """

    def _pages(self):
        pages = [each for each in routed() if getattr(each.pattern.callback, "login_required", True)]
        self.assertTrue(pages, "No routed page was found")
        return pages

    def test_a_page_that_takes_a_post_names_the_commands_it_runs(self):
        """So every write a page takes runs a declared command, authorized in
        the shared layer. The host's routes, each installed extension's and the
        example plugin's. A route that answers without a session
        (``authorizes_itself``: sign-in, sign-out) has nobody to run a command
        as, so it is no page this holds."""

        from hq.platform.application.pages import declares_runs
        from tests.fixtures.example_hq_plugin.plugin import plugin as example

        example_pages = [
            each
            for each in routed(import_module(example.urlconf).urlpatterns)
            if getattr(each.pattern.callback, "login_required", True)
        ]
        silent = [
            f"{each.path} {each.view.__module__}.{each.view.__qualname__}"
            for each in (*self._pages(), *example_pages)
            if _takes_post(each.pattern.callback) and not declares_runs(each.pattern.callback)
        ]
        self.assertEqual(silent, [], "these take a POST and name no command they run")
