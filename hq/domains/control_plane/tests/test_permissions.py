"""Every permission HQ asks an operator for comes from its provider's table.

The tables in ``control_plane.permissions`` say each name the way the
provider's screen says it, with its API name beside it where the two differ.
A name written as a literal anywhere else is one the operator may not find on
that screen, so the source walk here fails it.
"""

import ast
import json
import re
from collections.abc import Iterable
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.apart import apart
from hq.platform.application.tests.reads import reads

ROOT = Path(__file__).resolve().parents[4]
# Where HQ's Python lives, walked but for each package's tests.
PACKAGES = ("hq", "tools")
TABLES = Path("hq/domains/control_plane/permissions.py")
# How each provider spells a permission: a Cloudflare group and its scope, a
# Tailscale scope, a GitHub App or Nginx Proxy Manager section and its level.
SHAPES = (
    r"\b(Read|Write|Edit) \((account|zone)\)",
    r"^[a-z_]+(:[a-z_]+)*:read$",
    r"^[A-Za-z0-9][\w ]*: (read|write|view|Read-only|Read & write|View Only)\b",
)


def literals(root: str, texts: list[str]) -> list[str]:
    """Each string constant outside the tables and tests that is shaped like a
    permission or is one of ``texts``, as ``path:line: text``."""

    shapes = [re.compile(shape) for shape in SHAPES]
    exact = set(texts)
    return [
        f"{relative}:{line}: {text!r}"
        for relative in _walked(Path(root))
        for line, text in _strings(Path(root) / relative)
        if text in exact or any(shape.search(text) for shape in shapes)
    ]


def _walked(root: Path) -> list[Path]:
    found = (path.relative_to(root) for package in PACKAGES for path in sorted((root / package).rglob("*.py")))
    return [relative for relative in found if relative != TABLES and "tests" not in relative.parts]


def _strings(path: Path) -> list[tuple[int, str]]:
    nodes = ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    return [
        (node.lineno, node.value) for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def asked(provider: str) -> tuple[str, ...]:
    """Every permission HQ names to an operator for one connection provider."""

    from ..connection_kinds import CONNECTION_KINDS
    from ..credential_reads import observer_permissions

    return (*observer_permissions(provider), *CONNECTION_KINDS[provider].writes)


@tag("source", "tree")
@reads("**/*.py", "controller/providers/*.go", lists=("*",))
class PermissionLiteralTests(SimpleTestCase):
    def assert_none(self, found: Iterable[str]) -> None:
        self.assertEqual(
            list(found),
            [],
            "A permission is a member of its provider's table in control_plane.permissions.",
        )

    def test_no_permission_is_spelled_outside_its_table(self):
        self.assert_none(apart(literals, ROOT, []))

    def test_no_member_of_a_table_is_copied_as_text(self):
        from ..permissions import Permission

        texts = [text for table in Permission.__subclasses__() for member in table for text in (member, member.api)]
        self.assert_none(apart(literals, ROOT, texts))

    def test_every_scope_the_controller_names_is_in_its_table(self):
        """The controller's refusal names the scope a read needs; it is Go, so
        it cannot take the member, and is held to it here."""
        from ..permissions import Tailscale

        named = {
            scope
            for path in sorted((ROOT / "controller" / "providers").glob("*.go"))
            if not path.name.endswith("_test.go")
            for scope in re.findall(r'"([a-z_]+(?::[a-z_]+)*:read)"', path.read_text(encoding="utf-8"))
        }
        self.assertTrue(named)
        self.assertLessEqual(named, set(Tailscale))

    def test_the_walk_finds_a_permission_written_out(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as folder:
            module = Path(folder) / "hq" / "example.py"
            module.parent.mkdir()
            module.write_text('NEEDS = ("Example Read (zone)", "example:read", "Example: Read-only", "spelled")\n')

            found = apart(literals, folder, ["spelled"])

        self.assertEqual(len(found), 4, found)


class PermissionTableTests(SimpleTestCase):
    def test_each_provider_takes_every_name_from_one_table(self):
        from ..connection_kinds import CONNECTION_KINDS
        from ..permissions import Permission

        for provider in CONNECTION_KINDS:
            with self.subTest(provider=provider):
                tables = {type(name) for name in asked(provider)}
                self.assertLessEqual(len(tables), 1, tables)
                self.assertTrue(all(issubclass(table, Permission) for table in tables), tables)

    def test_cloudflare_shows_the_dashboard_name_beside_the_api_name(self):
        from ..permissions import Cloudflare

        self.assertEqual(Cloudflare.D1_EDIT.api, "D1 Write (account)")
        self.assertIn("D1 Edit", Cloudflare.D1_EDIT)
        self.assertIn("D1 Write", Cloudflare.D1_EDIT)
        self.assertEqual(Cloudflare.D1_READ, Cloudflare.D1_READ.api)

    @tag("source")
    def test_every_github_permission_is_one_the_app_is_registered_with(self):
        """GitHub refuses a token asking for more than the App declares."""

        declared = json.loads((ROOT / "deploy" / "github-apps.json").read_text())["apps"]["hq"]["permissions"]
        levels = ("read", "write")
        for name in asked("github_app"):
            with self.subTest(name=name):
                key, level = name.api.split(": ")
                self.assertIn(key, declared)
                self.assertGreaterEqual(levels.index(declared[key]), levels.index(level))
