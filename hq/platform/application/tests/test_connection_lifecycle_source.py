"""A connection's lifecycle is said in one module, and every view reads it there."""

import ast
import re
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from ..connection_lifecycle import UNREPORTED_FACT, UNREPORTED_NODE, Lifecycle
from .facts import Found, scan

HOME = Path(__file__).resolve().parents[1] / "connection_lifecycle.py"
SOURCE = Path(__file__).resolve().parents[3]
VALUES = {str(state) for state in Lifecycle}
LABEL = Lifecycle.UNREPORTED.label.lower()


def _content(pattern: str) -> dict:
    """A string with a literal part that ``pattern`` matches."""

    return {"kind": "string", "has": {"kind": "string_content", "regex": pattern}}


# A collection with a state's value among its items or its keys, and each
# literal part of a string, implicit concatenations whole, that spells the
# unreported marker.
STATE = _content(f"^({'|'.join(map(re.escape, sorted(VALUES)))})$")
RULES = {
    "listing": {
        "any": [{"kind": kind} for kind in ("dictionary", "list", "set", "tuple", "expression_list")],
        "has": {"any": [STATE, {"kind": "pair", "has": {"field": "key", **STATE}}]},
    },
    "spelled": {
        "kind": "string_content",
        "regex": f"(?i)^{re.escape(LABEL)}$|{re.escape(UNREPORTED_FACT)}|{re.escape(UNREPORTED_NODE)}",
    },
    "joined": {"kind": "concatenated_string"},
}


def _modules(found: list[Found]) -> list[Found]:
    return [each for each in found if not {"tests", "migrations"} & set(each.path.parts) and each.path != HOME]


def _named(listing: Found) -> set[str]:
    """The states a collection's items, or a dictionary's keys, spell."""

    node = ast.parse(f"({listing.text})", mode="eval").body
    items = node.keys if isinstance(node, ast.Dict) else getattr(node, "elts", [])
    return {item.value for item in items if isinstance(item, ast.Constant) and item.value in VALUES}


def _spells(joined: Found) -> bool:
    text = joined.value()
    return isinstance(text, str) and (text.lower() == LABEL or UNREPORTED_FACT in text or UNREPORTED_NODE in text)


def restated(*paths: Path) -> dict[str, list[str]]:
    """Where a module other than the lifecycle's lists two states or spells the unreported marker."""

    found: dict[str, set[str]] = {"listing": set(), "spelled": set()}
    for each in _modules(scan(RULES, *paths)):
        if each.rule == "listing" and len(_named(each)) > 1:
            found["listing"].add(f"{each.path.name}:{each.line}")
        elif each.rule == "spelled" or (each.rule == "joined" and _spells(each)):
            found["spelled"].add(f"{each.path.name}:{each.line}")
    return {kind: sorted(where) for kind, where in found.items()}


@tag("source", "tree")
class LifecycleDeclaredOnceTests(SimpleTestCase):
    def test_no_other_module_lists_lifecycle_states_or_spells_the_unreported_marker(self):
        """A label table or a set of states spelled as strings restates the vocabulary."""

        self.assertEqual(restated(SOURCE), {"listing": [], "spelled": []})

    def test_a_restated_state_or_marker_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "example.py"
            module.write_text(
                f'LABELS = {{"{Lifecycle.READY}": "Up", "{Lifecycle.STALE}": "Old"}}\n'
                f'WORKING = "{Lifecycle.READY}", "{Lifecycle.REACHABLE}"\n'
                f'ONE = ("{Lifecycle.READY}", "other")\n'
                f'"""Marks {UNREPORTED_FACT}."""\n'
                f'SAID = f"{{name}}{LABEL.upper()}"\n'
                f'NODE = "{UNREPORTED_NODE[:5]}" "{UNREPORTED_NODE[5:]}"\n'
            )

            self.assertEqual(
                restated(module),
                {
                    "listing": ["example.py:1", "example.py:2"],
                    "spelled": ["example.py:4", "example.py:5", "example.py:6"],
                },
            )

    def test_the_markers_keep_the_values_already_stored(self):
        self.assertEqual(UNREPORTED_FACT, "connection-unreported")
        self.assertEqual(UNREPORTED_NODE, "connection:unreported:")
