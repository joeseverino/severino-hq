"""What a person reads about an action is declared once and read from there.

A command's words live on its declaration (``CapabilitySpec.label``, ``verb``
and ``variants``, ``OutboundWork.label`` and ``verb``, the
``OperationRequest.Action`` choices). Every button, link, remedy and toast
reads them, so the words cannot drift apart again: these tests fail on text a
call site or a template writes for itself, and on a call site that names words
the declaration does not have.
"""

import ast
import inspect
import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.domains.control_plane.models import OperationRequest

from ..action_links import capability_action_link, command_action, read_now_link
from ..capabilities import capability_registry
from ..item_help import remedy_link
from ..outbound_work import ask, declared_work
from .source import parsed, source_files, template_files

ROOT = Path(__file__).resolve().parents[4]
# The one module allowed to put a capability on a link: it reads the words.
LINK_BUILDER = "hq/platform/application/action_links.py"
# Constructors of a control a person presses, and where their label sits.
LABEL_POSITION = {"Ask": 0, "PageAction": 0, "ActionLink": 1, "TopologyAction": 1}
# What builds a control for a command or a piece of outbound work.
COMMAND_BUILDERS = {
    builder.__name__: builder for builder in (ask, command_action, capability_action_link, read_now_link, remedy_link)
}
# Text a template writes that is a declared word for something no command does.
TEMPLATE_EXEMPT = {
    ("core/_dashboard_glance.html", "Refresh"): "fetches the glance panels again; no command runs",
    ("dashboard.html", "Edit"): "opens the chooser of the dashboard's links, whose save is a preference",
}


def _modules():
    for path in source_files(ROOT / "hq", "*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if "tests" in path.parts or path.name.startswith("test") or path.name.endswith("_tests.py"):
            continue
        yield relative, parsed(path)


def _called(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _label(node: ast.Call) -> ast.expr | None:
    position = LABEL_POSITION.get(_called(node))
    if position is None:
        return None
    named = next((keyword.value for keyword in node.keywords if keyword.arg == "label"), None)
    return named if named is not None else (node.args[position] if len(node.args) > position else None)


def _confirm_label(node: ast.Dict) -> ast.expr | None:
    """The button of a confirmation page (``confirm_page.html``): the
    ``label`` of a dict that also says where Cancel goes."""

    keys = {
        key.value: value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant)
    }
    return keys.get("label") if "cancel_url" in keys else None


def _labels(node: ast.AST) -> list[ast.expr]:
    """What a control built at ``node`` says: a constructor's label or intent,
    or a confirmation's button."""

    if isinstance(node, ast.Dict):
        return [label] if (label := _confirm_label(node)) is not None else []
    if not isinstance(node, ast.Call):
        return []
    said = [keyword.value for keyword in node.keywords if keyword.arg == "intent"]
    return [label, *said] if (label := _label(node)) is not None else said


def _restated(label: ast.expr, words: set[str]) -> list[str]:
    """Declared words a label writes out, on either side of a choice included."""

    return [
        part.value
        for part in ast.walk(label)
        if isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value in words
    ]


def _written(node: ast.expr | None) -> bool:
    """Text the call site spells out itself, rather than reads from somewhere."""

    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def _reaches_a_command(node: ast.Call) -> bool:
    """Whether the control posts to or opens a command: its URL is built by
    ``command_url`` or is the route outbound work is asked through."""

    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call) or inner is node:
            continue
        if _called(inner) == "command_url":
            return True
        if _called(inner) == "reverse" and inner.args and getattr(inner.args[0], "value", None) == "jobs:ask":
            return True
    return False


def _toast(node: ast.Call) -> list[ast.expr]:
    """What a call says to the person who pressed: a ``messages`` call's text,
    or the ``message`` an answer to an ask carries."""

    said = [keyword.value for keyword in node.keywords if keyword.arg == "message"]
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "messages":
        said += node.args[1:2]
    return said


def _declared_words() -> set[str]:
    words = {choice.label for choice in OperationRequest.Action}
    words |= {work.words for work in declared_work().values()}
    for spec in capability_registry().values():
        words |= {spec.words(), *(text for _name, text in spec.variants)}
    return words


def pressed_words(text: str, words: set[str]) -> list[str]:
    """The declared words a sentence tells a person to press, written out.
    Only after "press", "click" or "tap", and only in a declared word's own
    capitals, so ordinary prose that shares a word is never one."""

    named = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    pattern = re.compile(rf"\b(?:[Pp]ress|[Cc]lick|[Tt]ap)\s+[\"“]?({named})(?![\w-])")
    return pattern.findall(text)


@tag("source", "tree")
class CallSiteWordsTests(SimpleTestCase):
    """No button, link or toast for a command brings text of its own."""

    def test_only_the_link_builder_puts_a_capability_on_a_link(self):
        found = [
            f"{relative}:{node.lineno}"
            for relative, tree in _modules()
            if relative != LINK_BUILDER
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _called(node) in ("ActionLink", "TopologyAction")
            and any(keyword.arg == "capability" for keyword in node.keywords)
        ]
        self.assertEqual(found, [], "build it with action_links.command_action, which reads the command's words")

    def test_no_builder_of_a_commands_control_takes_words(self):
        """The builders read the declaration, so none of them has a ``label``
        for a caller to override it with."""

        found = [
            f"{relative}:{node.lineno}"
            for relative, tree in _modules()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _called(node) in COMMAND_BUILDERS
            and any(keyword.arg == "label" for keyword in node.keywords)
        ]
        self.assertEqual(found, [])
        for builder in COMMAND_BUILDERS.values():
            with self.subTest(builder.__name__):
                self.assertNotIn("label", inspect.signature(builder).parameters)

    def test_a_control_that_reaches_a_command_says_the_commands_words(self):
        found = [
            f"{relative}:{node.lineno}"
            for relative, tree in _modules()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _written(_label(node)) and _reaches_a_command(node)
        ]
        self.assertEqual(found, [], "read the label from the declaration: capability_words, or the work's ask()")

    def test_no_control_restates_a_declared_word(self):
        words = _declared_words()
        found = [
            f"{relative}:{node.lineno}: {said!r}"
            for relative, tree in _modules()
            for node in ast.walk(tree)
            for label in _labels(node)
            for said in _restated(label, words)
        ]
        self.assertEqual(found, [], "a declared word is read from its declaration, never written again")

    def test_a_restated_word_is_found_in_each_way_a_control_is_written(self):
        words = {"Remove", "Stop tracking in HQ"}
        tree = ast.parse(
            'PageAction("Remove", url)\n'
            'ActionLink("x", "Stop tracking in HQ" if forget else words, url)\n'
            'confirm = {"url": url, "label": "Stop tracking in HQ" if forget else "Remove", "cancel_url": back}\n'
            'row = {"label": "Remove", "value": 1}\n'
            'PageAction(capability_words("infrastructure.resource.remove"), url)\n'
        )
        found = sorted(
            (node.lineno, said)
            for node in ast.walk(tree)
            for label in _labels(node)
            for said in _restated(label, words)
        )
        self.assertEqual(found, [(1, "Remove"), (2, "Stop tracking in HQ"), (3, "Remove"), (3, "Stop tracking in HQ")])

    def test_a_toast_says_requested_in_the_one_form(self):
        """``integration_specs.requested`` is the sentence every press answers with."""

        found = [
            f"{relative}:{node.lineno}"
            for relative, tree in _modules()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for text in _toast(node)
            if _written(text) and "requested" in ast.unparse(text)
        ]
        self.assertEqual(found, [])

    def test_no_template_writes_a_declared_word_as_a_control(self):
        words = _declared_words()
        control = re.compile(r"<(button|a)\b(?![^>]*\btype=\"button\")[^>]*>\s*([^<{]+?)\s*</\1>")
        templates = template_files()
        self.assertTrue(templates)
        found = sorted(
            (name, text)
            for name, path in templates
            for _element, text in control.findall(path.read_text(encoding="utf-8"))
            if text in words
        )
        self.assertEqual([item for item in found if item not in TEMPLATE_EXEMPT], [], '{% words "name" %} says it')

    def test_no_template_sentence_tells_a_person_to_press_a_written_word(self):
        """Prose that says which button to press names it with ``{% words %}``,
        so the sentence still matches the button when its words change."""

        found = sorted(
            f"{name}: {named!r}"
            for name, path in template_files()
            for named in pressed_words(path.read_text(encoding="utf-8"), _declared_words())
        )
        self.assertEqual(found, [], '{% words "name" %} says it')

    def test_every_template_exemption_still_exempts_something(self):
        for (template, text), reason in TEMPLATE_EXEMPT.items():
            with self.subTest(template):
                self.assertTrue(reason)
                self.assertIn(f">{text}<", dict(template_files())[template].read_text(encoding="utf-8"))


@tag("source", "tree")
class DeclaredWordsTests(SimpleTestCase):
    """Every action has the words a surface asks it for."""

    VARIANT_CALLS = frozenset({"capability_words", "command_action", "capability_action_link", "FindingRemedy"})

    def test_every_command_hq_declares_is_named_for_a_person(self):
        unnamed = [
            name
            for name, spec in capability_registry().items()
            if spec.handler.__module__.startswith("hq.") and not spec.label
        ]
        self.assertEqual(unnamed, [])

    def test_every_variant_a_call_site_names_is_declared(self):
        """``words("all")`` on a command that declares no "all" is a KeyError
        at the press; here it is a failure before it ships."""

        registry = capability_registry()
        asked = set()
        for relative, tree in _modules():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _called(node) not in self.VARIANT_CALLS:
                    continue
                literals = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
                name = next((value for value in literals if value in registry), None)
                variant = next(
                    (getattr(keyword.value, "value", None) for keyword in node.keywords if keyword.arg == "variant"),
                    literals[1] if _called(node) == "capability_words" and len(literals) > 1 else None,
                )
                if name and isinstance(variant, str) and variant:
                    asked.add((relative, name, variant))
        tag_use = re.compile(r'\{%\s*words\s+"([^"]+)"(?:\s+"([^"]+)")?\s*%\}')
        for template, path in template_files():
            for name, variant in tag_use.findall(path.read_text(encoding="utf-8")):
                self.assertIn(name, registry, f"{template} names a command that is not declared")
                if variant:
                    asked.add((template, name, variant))
        self.assertTrue(asked)
        missing = [item for item in sorted(asked) if item[2] not in dict(registry[item[1]].variants)]
        self.assertEqual(missing, [])
