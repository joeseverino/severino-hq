"""What every page and template says the one shared way: titles, cells, counts, wording, buttons, trails and reads."""

import ast
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, tag

from hq.platform.application.routes import routed
from hq.platform.application.security import operator_capabilities
from hq.platform.core.middleware import LoginRequiredMiddleware
from hq.platform.core.test_runner import no_default_role
from hq_sdk import validation
from hq_sdk.validation import _cell_markup_pins

from .source import parsed, source_files, template_files
from .test_view_writes import routed_modules, shown


@tag("source")
class TemplateCommentTests(SimpleTestCase):
    """Django's {# #} comment is single-line only.

    A multi-line one is not a comment: it renders verbatim into the page. The
    failure is invisible in review because it looks exactly like a comment.
    The rule is ``interface_text.open_comment_lines``; ``InterfaceTextTests``
    holds every template of HQ and of each installed extension to it.
    """

    def test_a_comment_closed_on_its_line_is_fine(self):
        from hq.platform.core.interface_text import open_comment_lines

        self.assertEqual(open_comment_lines("<p>{# one #} and {# two #}</p>\n"), [])

    def test_a_comment_left_open_is_found_on_the_line_that_opens_it(self):
        from hq.platform.core.interface_text import open_comment_lines

        source = "<p>{# closed #} fine</p>\n<p>{# closed #} {# left\n   open #}</p>\n"

        self.assertEqual(open_comment_lines(source), [2])

    def test_a_template_root_is_read_whole(self):
        from hq.platform.core.interface_text import unclosed_template_comments

        with TemporaryDirectory() as root:
            Path(root, "partials").mkdir()
            Path(root, "page.html").write_text("<p>{# fine #}</p>\n", encoding="utf-8")
            Path(root, "partials", "_row.html").write_text("<td>\n{# left\n   open #}</td>\n", encoding="utf-8")

            self.assertEqual(unclosed_template_comments(root), ["partials/_row.html:2"])

    def test_the_sdk_exports_the_same_walk(self):
        from hq.platform.core import interface_text
        from hq_sdk import testing

        self.assertIs(testing.unclosed_template_comments, interface_text.unclosed_template_comments)


@tag("source", "tree")
class PageTitleTests(SimpleTestCase):
    """A page names itself; the site name is appended once, by the layout.

    Left to each page, a title either omits the site name or hardcodes a string
    ``SEVERINO_SITE_NAME`` is allowed to change. A title is not visible from
    inside the page that has it, so neither shows up in review.
    """

    def title_blocks(self):

        pattern = re.compile(r"\{%\s*block title\s*%\}(.*?)\{%\s*endblock", re.DOTALL)
        for name, template in template_files():
            if name == "base.html":
                continue
            match = pattern.search(template.read_text(encoding="utf-8"))
            if match:
                yield name, match.group(1).strip()

    def test_no_page_appends_the_site_name_itself(self):
        offenders = [
            f"{name}: {leaf}" for name, leaf in self.title_blocks() if "SITE_NAME" in leaf or "Severino HQ" in leaf
        ]
        self.assertEqual(
            offenders,
            [],
            "base.html appends the site name: a page block names only itself",
        )

    def test_every_page_names_itself(self):
        offenders = [str(name) for name, leaf in self.title_blocks() if not leaf]
        self.assertEqual(
            offenders,
            [],
            "an empty title block renders as the site name preceded by a separator",
        )

    def test_the_layout_is_what_appends_it(self):
        base = Path(__file__).resolve().parents[4] / "templates" / "base.html"
        self.assertIn(
            "{% block title %}{% endblock %} · {{ SITE_NAME }}",
            base.read_text(encoding="utf-8"),
        )


@tag("source", "tree")
class CellMarkupTests(SimpleTestCase):
    """A test reads a table cell by its text: the host may wrap what a cell says.

    One rule for the host and its extensions: ``hq_sdk.validation`` refuses it
    in an extension's tree, and this holds the host's own tests to it.
    """

    def test_no_test_pins_a_cell_closing_tag(self):
        root = Path(__file__).resolve().parents[4]
        offenders = [
            f"{path.relative_to(root)}:{line}"
            for path in source_files(root, "*.py")
            for line in _cell_markup_pins(parsed(path))
        ]
        self.assertEqual(offenders, [], "read the cell's text, not the tag that closes it")

    def test_a_pin_is_found_however_the_text_is_written(self):
        pinned = {
            "a literal": 'self.assertContains(response, "<td>Served</td>")',
            "a header": 'self.assertNotContains(response, "<th>Name</th>\\n")',
            "a keyword": 'self.assertContains(response, text="<td>Served</td>")',
            "an f-string": 'self.assertIn(f"<td>{name}</td>", html)',
            "a concatenation": 'self.assertNotIn("<td>" + name + "</td>", html)',
        }
        free = {
            "a cell's text": 'self.assertIn("Served", cells)',
            "an opening tag": 'self.assertContains(response, "<td>")',
            "a computed end": 'self.assertIn(f"<td>{tail}", html)',
            "the haystack": 'self.assertIn("Served", "<td>Served</td>")',
            "another assertion": 'self.assertEqual(cell, "<td>Served</td>")',
        }
        for name, line in pinned.items():
            with self.subTest(pinned=name):
                self.assertEqual(_cell_markup_pins(ast.parse(line)), [1])
        for name, line in free.items():
            with self.subTest(free=name):
                self.assertEqual(_cell_markup_pins(ast.parse(line)), [])

    def test_the_plugin_check_refuses_a_pin_and_says_what_to_read(self):
        with TemporaryDirectory() as source:
            tests = Path(source) / "example_plugin" / "tests"
            tests.mkdir(parents=True)
            (tests / "test_pages.py").write_text('def test(self, r):\n    self.assertContains(r, "<td>A</td>")\n')
            with patch("sys.argv", ["validation", source]), patch("builtins.print") as printed:
                self.assertEqual(validation.main(), 1)
        said = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("example_plugin/tests/test_pages.py:2", said)
        self.assertIn("read the cell's text", said)


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
        self.assertEqual(rendered, "1 thing needs you")


class InterfaceTextTests(SimpleTestCase):
    """What HQ and its extensions show people, read from source in one pass.

    A gate reads it, here. A running HQ never does: a system check runs before
    every management command, at every container start.
    """

    @classmethod
    def setUpClass(cls):
        from hq.platform.core import interface_text

        super().setUpClass()
        cls.reading = interface_text.read()

    def _places(self, found):
        return [f"{path}:{line}" + (f": {why[0]}" if why else "") for path, line, *why in found]

    def test_the_pass_read_the_source(self):
        self.assertGreater(self.reading.files, 300, "The interface text pass read almost nothing")

    def test_no_plural_built_by_hand(self):
        """Use counted(n, one, many) or {{ n|counted:"one,many" }}."""
        self.assertEqual(self._places(self.reading.hand_plurals), [])

    def test_no_form_opened_inside_another(self):
        """Close the outer form first, or point the control at a form with form="id"."""
        self.assertEqual(self._places(self.reading.nested_forms), [])

    def test_no_template_comment_is_left_open(self):
        """A {# #} comment ends on its own line. Use {% comment %}…{% endcomment %} for more."""
        self.assertEqual(self._places(self.reading.open_comments), [])

    def test_every_counted_phrase_agrees(self):
        """Give both forms: counted(n, "zone band", "zone bands")."""
        self.assertEqual(self._places(self.reading.unagreeable_counts), [])

    def test_no_system_check_that_runs_by_default_reads_source(self):
        import inspect

        from django.core.checks.registry import registry

        reads_source = re.compile(r"\b(ast\.parse|rglob|os\.walk|read_text|interface_text)\b")
        checked = 0
        for check in registry.get_checks(include_deployment_checks=False):
            module = inspect.getmodule(check)
            if module is None or not module.__name__.startswith("hq"):
                continue
            checked += 1
            with self.subTest(check=f"{module.__name__}.{check.__name__}"):
                self.assertIsNone(reads_source.search(inspect.getsource(module)))
        self.assertGreater(checked, 0, "No HQ system check was examined")


class PostButtonTests(SimpleTestCase):
    """A post_button submits the one shared form, so it needs none of its own."""

    def render(self, source, **context):
        from django.template import Context, Template

        return Template(source).render(Context(context))

    def test_it_targets_the_shared_form_with_its_own_action(self):
        html = self.render('{% post_button "↑" "/things/7/" value="up" title="Move up" %}')
        self.assertIn('form="hq-post"', html)
        self.assertIn('formaction="/things/7/"', html)
        self.assertIn('name="action" value="up"', html)
        self.assertIn('aria-label="Move up"', html)
        self.assertNotIn("disabled", html)

    def test_a_button_of_words_is_named_by_them(self):
        html = self.render('{% post_button "Check again" "/check/" title="Read the record again, then check it" %}')
        self.assertIn('title="Read the record again, then check it"', html)
        self.assertNotIn("aria-label", html)

    def test_it_can_be_disabled(self):
        html = self.render('{% post_button "Refresh" "/sync/" disabled=True %}')
        self.assertIn(" disabled>", html)

    def test_its_label_is_escaped(self):
        html = self.render("{% post_button label '/x/' %}", label="<b>")
        self.assertIn("&lt;b&gt;", html)


def _restated_trails(tree: ast.AST) -> list[int]:
    """Lines of a ``get_page_trail`` whose one statement returns a tuple of crumbs, starred parts and all."""

    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "get_page_trail"):
            continue
        body = [
            statement
            for statement in node.body
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        ]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        if isinstance(body[0].value, ast.Tuple):
            found.append(node.lineno)
    return found


@tag("source", "tree")
class PageTrailTests(SimpleTestCase):
    """A view says its trail with ``page_trail`` and ``trail_label``, not a ``get_page_trail`` that restates them.

    ``page_trail`` holds every crumb above the record, so a page several
    levels down declares them too, the pages above it spread in
    (``(*ABOVE, crumb)``). An override stays for a trail the mixin cannot say:
    one built from the request or from several statements.
    """

    def test_no_view_overrides_get_page_trail_to_say_what_the_mixin_says(self):
        root = Path(__file__).resolve().parents[4]
        self.assertEqual(_restated_trails(ast.parse("def get_page_trail(self):\n    return (A,)")), [1])
        self.assertEqual(_restated_trails(ast.parse("def get_page_trail(self):\n    return (*ABOVE, (L, U))")), [1])
        self.assertEqual(_restated_trails(ast.parse("def get_page_trail(self):\n    x = 1\n    return (x,)")), [])
        restated = [
            f"{shown(root, path)}:{line}" for path in routed_modules(root) for line in _restated_trails(parsed(path))
        ]
        self.assertEqual(restated, [], "declare page_trail (and trail_label) on the view")


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
        with no_default_role():
            nobody = get_user_model().objects.create_user("nobody")
        opened = [
            f"{each.path} {each.view.__module__}.{each.view.__qualname__}"
            for each in self._pages()
            if self._opened(nobody, each)
        ]
        self.assertEqual(opened, [], "these pages read without a capability")

    def test_each_page_reads_under_one_capability_an_operator_opens(self):
        from hq.platform.application.pages import page_capability

        operator = get_user_model().objects.create_user("operator")
        held = operator_capabilities()
        wrong = [
            each.path
            for each in self._pages()
            if page_capability(each.pattern.callback) not in held or not self._opened(operator, each)
        ]
        self.assertEqual(wrong, [])
