import ast
from pathlib import Path

from django.test import SimpleTestCase

from .security import AuthorizationError, Capability, Principal, require_all

READER = Principal("reader", "test", frozenset({Capability.READ}))


class RequireAllTests(SimpleTestCase):
    def test_every_capability_held_passes(self):
        require_all(READER, (Capability.READ,))
        require_all(READER, ())

    def test_one_capability_missing_is_refused(self):
        with self.assertRaises(AuthorizationError):
            require_all(READER, (Capability.READ, Capability.MANAGE_INFRASTRUCTURE))

    def test_no_registry_writes_the_loop_again(self):
        """Resources, capabilities, search and imports each looped over
        ``principal.require``; one rule written four times is four places to
        get it subtly different."""

        root = Path(__file__).resolve().parent
        found = []
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("test") or path.name == "security.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.For) and any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "require"
                    for statement in node.body
                    for call in ast.walk(statement)
                ):
                    found.append(f"{path.name}:{node.lineno}")
        self.assertEqual(found, [])
