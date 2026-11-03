"""Questions asked in many places, each answered by one primitive or one definition."""

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import parsed, source_files, template_files


@tag("source", "tree")
class OnePrimitiveTests(SimpleTestCase):
    """Questions asked in many places are answered by one function."""

    ROOT = Path(__file__).resolve().parents[4]
    # Modules another pass routes through the primitives.
    PENDING: set[str] = set()

    def sources(self, *packages):
        for package in packages:
            for path in sorted((self.ROOT / package).rglob("*.py")):
                relative = path.relative_to(self.ROOT).as_posix()
                if path.name.startswith("test") or relative in self.PENDING:
                    continue
                yield relative, path.read_text(encoding="utf-8")

    def test_hostnames_are_spelled_by_normalized_hostname(self):

        inline = re.compile(r'lower\(\)\s*\.rstrip\("\."\)|rstrip\("\."\)\s*\.lower\(\)')
        found = [
            relative
            for relative, text in self.sources(
                "hq/platform/application", "hq/domains/control_plane", "hq/platform/core"
            )
            if relative != "hq/domains/control_plane/names.py" and inline.search(text)
        ]
        self.assertEqual(found, [])

    def test_a_command_is_linked_through_command_url(self):
        """One builder, so a target is always encoded the way the form reads
        it: a hand-built ``?target=`` opened a form with nothing chosen."""

        inline = re.compile(r"""reverse\(\s*["']command["']""")
        scanned = list(
            self.sources(
                "hq/platform/application",
                "hq/domains/control_plane",
                "hq/platform/core",
                "hq/platform/api",
                "hq/platform/mcp",
            )
        )
        found = [
            relative
            for relative, text in scanned
            if relative != "hq/platform/application/action_links.py" and inline.search(text)
        ]
        self.assertIn("hq/platform/application/action_links.py", {relative for relative, _ in scanned})
        self.assertEqual(found, [])
        templates = [
            name
            for name, path in template_files()
            if re.search(r"""\{%\s*url\s+["']command["']""", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(templates, [])

    # Identifiers still written in bare code. Lower it as they move to
    # `|breakable`; never raise it.
    BARE_CODE_CEILING = 35

    def test_an_identifier_is_written_as_a_breakable_token(self):
        """A value in code wraps at its seams (`|breakable`), never inside
        "compose.y|aml" because its box lets prose break anywhere."""

        # A code block keeps its own lines, so code inside <pre> is not a token.
        bare = re.compile(r"(<pre[^>]*>\s*)?(<code[^>]*>\{\{(?:(?!\}\}).)*\}\})")
        found = [
            f"{name}: {code}"
            for name, path in template_files()
            for block, code in bare.findall(path.read_text(encoding="utf-8"))
            if not block and "|breakable" not in code
        ]
        self.assertLessEqual(len(found), self.BARE_CODE_CEILING, "\n".join(found))

    def test_zone_membership_is_asked_of_in_zone(self):

        inline = re.compile(r'endswith\(f"\.\{')
        found = [
            relative
            for relative, text in self.sources(
                "hq/platform/application", "hq/domains/control_plane", "hq/platform/core"
            )
            if relative != "hq/domains/control_plane/names.py" and inline.search(text)
        ]
        self.assertEqual(found, [])

    def test_templates_say_byte_counts_through_the_bytes_filter(self):
        found = [name for name, path in template_files() if "filesizeformat" in path.read_text(encoding="utf-8")]
        self.assertEqual(found, [])

    def test_templates_write_amounts_through_the_money_filter(self):
        """``{{ amount|money }}``, never a dollar sign in front of a number filter,
        which writes a negative amount as "$-5.00"."""

        built = re.compile(r"\$\{\{")
        found = [name for name, path in template_files() if built.search(path.read_text(encoding="utf-8"))]
        self.assertEqual(found, [])

    def test_a_related_list_is_drawn_by_its_partial(self):
        """A record page lists what it is related to through
        `partials/_related.html` (groups from `application.related`), never
        under a "Related" heading over a list of its own shape."""

        found = [
            name
            for name, path in template_files()
            if re.search(r"<h[1-6][^>]*>\s*Related\b", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [])

    def test_python_writes_amounts_through_money(self):
        """``application.money.money`` is the one place a dollar sign meets a
        number: no other f-string puts one in front of a value."""

        def builds_an_amount(path: Path) -> bool:
            for node in ast.walk(parsed(path)):
                if not isinstance(node, ast.JoinedStr):
                    continue
                for before, after in zip(node.values, node.values[1:], strict=False):
                    if (
                        isinstance(before, ast.Constant)
                        and str(before.value).endswith("$")
                        and isinstance(after, ast.FormattedValue)
                    ):
                        return True
            return False

        sources = [
            path
            for package in ("hq", "hq_sdk")
            for path in sorted((self.ROOT / package).rglob("*.py"))
            if "tests" not in path.parts
        ]
        self.assertTrue(sources)
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sources
            if path.relative_to(self.ROOT).as_posix() != "hq/platform/application/money.py" and builds_an_amount(path)
        ]
        self.assertEqual(found, [])

    def test_python_says_byte_counts_through_human_bytes(self):
        called = re.compile(r"import[^\n]*filesizeformat|filesizeformat\(")
        sources = [
            path
            for package in ("hq", "hq_sdk")
            for path in sorted((self.ROOT / package).rglob("*.py"))
            if "tests" not in path.parts
        ]
        self.assertTrue(sources)
        found = [
            path.relative_to(self.ROOT).as_posix()
            for path in sources
            if called.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [])

    def test_templates_name_entities_through_the_entity_tag(self):

        # A button to a page (Cancel, Edit) is an action, not a mention.
        mention = re.compile(
            r'<a href="\{% url \'(control_plane:(detail|machine|service)|zones:detail|'
            r"projects:detail)\'"
        )
        found = [name for name, path in template_files() if mention.search(path.read_text(encoding="utf-8"))]
        self.assertEqual(found, [])

    def test_entity_pages_are_addressed_by_entity_link(self):

        page = re.compile(
            r'reverse\(\s*"(control_plane:machine|control_plane:service|zones:detail|'
            r'projects:detail|control_plane:detail)"'
        )
        found = [
            relative
            for relative, text in self.sources("hq/platform/application")
            if relative != "hq/platform/application/entity_links.py"
            and not relative.endswith("views.py")
            and page.search(text)
        ]
        self.assertEqual(found, [])


@tag("source", "tree")
class OneDefinitionTests(SimpleTestCase):
    """A module-level name is assigned once.

    Python keeps the later of two assignments without a word, so a second
    constant of the same name quietly replaces the first everywhere it is read,
    a probe string included. Ruff's F811 covers functions and imports only.
    """

    def test_no_module_level_name_is_assigned_twice(self):
        root = Path(__file__).resolve().parents[4]
        twice = {}
        for path in source_files(root, "*.py"):
            counts: dict[str, int] = {}
            for node in parsed(path).body:
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                    if isinstance(node, ast.AnnAssign)
                    else []
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        counts[target.id] = counts.get(target.id, 0) + 1
            twice |= {f"{path.relative_to(root)}: {name}": n for name, n in counts.items() if n > 1}
        self.assertEqual(twice, {}, "give each its own name")


class RouteOwnerTests(SimpleTestCase):
    """Routes are resolved in one place, so the answer can be remembered."""

    @tag("source", "tree")
    def test_the_application_resolves_routes_through_its_one_owner(self):
        root = Path(__file__).resolve().parents[4]
        modules = [
            path
            for package in ("hq/platform/application", "hq/domains/control_plane")
            for path in (root / package).rglob("*.py")
            if "tests" not in path.parts and path.name != "routes.py"
        ]
        self.assertGreater(len(modules), 50, "the walk found almost nothing; its packages are not where it looks")
        offenders = [
            str(path.relative_to(root))
            for path in modules
            if re.search(r"django\.urls import[^\n]*\breverse\b", path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(offenders, [], "import reverse from application.routes")

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

    ROOT = Path(__file__).resolve().parents[4]
    PRIMITIVE = "fragment.js"

    def test_the_primitive_loads_before_the_scripts_that_use_it(self):
        base = (self.ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        deferred = re.findall(r"js/([\w-]+\.js)'[^>]*\bdefer\b", base)

        self.assertGreater(len(deferred), 1)
        self.assertEqual(deferred[0], self.PRIMITIVE)

    def test_every_part_a_template_names_is_one_a_template_defines(self):
        sources = [path.read_text(encoding="utf-8") for _name, path in template_files()]
        named = {name for text in sources for name in re.findall(r'data-fragment-name="(\w+)"', text)}
        defined = {name for text in sources for name in re.findall(r"{% partialdef (\w+)", text)}

        self.assertTrue(named)
        self.assertEqual(named - defined, set())
