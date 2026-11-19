"""What every page and template says the one shared way: titles, counts, wording, buttons, trails and reads."""

import re
from importlib import import_module
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, tag

from hq.platform.application.routes import routed
from hq.platform.application.security import operator_capabilities
from hq.platform.application.tests.reads import reads
from hq.platform.core.middleware import LoginRequiredMiddleware
from hq.platform.core.testing import person
from hq_sdk.testing import plain

from .facts import scan
from .test_view_writes import routed_modules, shown


class CountedTests(SimpleTestCase):
    """One way to say how many, so a noun and its verb agree."""

    def test_the_phrase_agrees_for_one_and_for_many(self):
        from hq.platform.application.ui import counted

        self.assertEqual(counted(1, "needs output", "need output"), "1 needs output")
        self.assertEqual(counted(3, "needs output", "need output"), "3 need output")
        self.assertEqual(counted(0, "change"), "0 changes")
        self.assertEqual(counted(1200, "change"), "1,200 changes")

    def test_a_phrase_without_its_plural_is_refused(self):
        from hq.platform.application.ui import counted

        with self.assertRaises(ValueError):
            counted(2, "needs output")

    def test_the_template_filter_is_the_same_rule(self):
        from django.template import Context, Template

        rendered = Template('{{ n|counted:"thing needs you,things need you" }}').render(Context({"n": 1}))
        self.assertEqual(plain(rendered), "1 thing needs you")


class SystemCheckTests(SimpleTestCase):
    """A system check runs before every management command, at every container
    start, so none that runs by default reads source."""

    def test_no_system_check_that_runs_by_default_reads_source(self):
        import inspect

        from django.core.checks.registry import registry

        reads_source = re.compile(r"\b(ast\.parse|rglob|os\.walk|read_text)\b")
        checked = 0
        for check in registry.get_checks(include_deployment_checks=False):
            module = inspect.getmodule(check)
            if module is None or not module.__name__.startswith("hq"):
                continue
            checked += 1
            with self.subTest(check=f"{module.__name__}.{check.__name__}"):
                self.assertIsNone(reads_source.search(inspect.getsource(module)))
        self.assertGreater(checked, 0, "No HQ system check was examined")


@tag("source", "tree")
@reads("**/*.py", "!**/tests/**/test_*.py", "tools/lint/ast-grep/rules/page-trail-declared.yml")
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

    def _opened(self, user, each) -> bool:
        request = RequestFactory().get(each.path)
        request.user = user
        try:
            LoginRequiredMiddleware(lambda _request: HttpResponse()).process_view(
                request, each.pattern.callback, (), {}
            )
        except PermissionDenied:
            return False
        return True

    def test_a_person_who_holds_nothing_opens_no_page(self):
        nobody = person(username="nobody")
        opened = [
            f"{each.path} {each.view.__module__}.{each.view.__qualname__}"
            for each in self._pages()
            if self._opened(nobody, each)
        ]
        self.assertEqual(opened, [], "these pages read without a capability")

    def test_each_page_reads_under_capabilities_an_operator_opens(self):
        from hq.platform.application.pages import page_capabilities

        operator = get_user_model().objects.create_user("operator")
        held = operator_capabilities()
        wrong = [
            each.path
            for each in self._pages()
            if not held.issuperset(page_capabilities(each.pattern.callback)) or not self._opened(operator, each)
        ]
        self.assertEqual(wrong, [])

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

    def test_a_page_an_extension_mounts_reads_under_its_own_capability_and_what_it_runs_needs(self):
        """Never under the host's READ alone, nor under less than a command it
        runs requires. Each installed extension's routes, and the example
        plugin's."""

        from hq.platform.application.capabilities import capability_registry
        from hq.platform.application.pages import declared_runs, page_capabilities
        from hq.platform.application.plugins import installed_plugins
        from tests.fixtures.example_hq_plugin.plugin import plugin as example

        extensions = {plugin.id: plugin for plugin in (example, *installed_plugins()) if plugin.urlconf}
        registry = capability_registry()
        wrong = []
        for extension in extensions.values():
            own = set(extension.operator_capabilities)
            for each in routed(import_module(extension.urlconf).urlpatterns):
                reads_under = set(page_capabilities(each.pattern.callback))
                if not reads_under & own:
                    wrong.append(f"{extension.id} {each.path}: reads under none of {sorted(own)}")
                for name, _variant in declared_runs(each.pattern.callback):
                    if needs := set(registry[name].required_capabilities) - reads_under:
                        wrong.append(f"{extension.id} {each.path}: runs {name}, which needs {sorted(needs)}")
        self.assertEqual(wrong, [])
