"""A sheet's rules style shared parts, never a part only one page draws.

A part one page alone draws is a job a shared primitive should do: its rules
are a page styled by hand, which the next page needing the same job copies.
So every rule that names a part's class is that of a part drawn in more than
one place, or one an extension may write too (``public``). A part that is one
page's own drawing, a job no other page has, says so (``drawing_of``), and is
held to it: that page is the one place it is drawn.
"""

import re
from collections.abc import Callable, Iterable
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.core.hand_drawn import Source
from hq.platform.core.template_contract import FRAMES, parts
from hq.platform.core.tests.css_usage import rules_naming

ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = ROOT / "templates"
# Modules that declare, list or check the parts, which name a partial without drawing a page.
_DECLARING = (
    "hq/platform/core/hand_drawn.py",
    "hq/platform/core/included_actions.py",
    "hq/platform/core/included_parts.py",
    "hq/platform/core/template_contract.py",
    "hq/platform/core/template_engine.py",
    "hq/platform/core/part_examples/",
    "hq/platform/core/templatetags/",
)
_EXTENDS = re.compile(r"\{%\s*extends\b.*?%\}", re.DOTALL)
# What a frame draws, which every page extends, reaches every page, as does
# what Django's form renderer draws for every form.
_EVERY = "every page"
_RENDERED = "django/forms/"


def _places(templates: dict[str, str], modules: dict[str, str], every: Iterable) -> Callable[[str], set[str]]:
    """Where a template is drawn: each page template and view module that draws
    it, through every part and partial between."""

    owner = {name: part for part in every for name in part.files()}
    included = {name: _EXTENDS.sub("", Source(text).text) for name, text in templates.items()}
    frames = {"base.html", *FRAMES}
    seen: dict[str, set[str]] = {}

    def drawers(name: str) -> set[str]:
        part = owner.get(name)
        own = set(part.files()) if part else {name}
        partial = part.partial.partition("#")[0] if part else name
        written = rf"[\"']{re.escape(partial)}(?:#[\w-]+)?[\"']"
        if part and part.name:
            written += rf"|\{{%\s*{re.escape(part.name)}\b"
        pattern = re.compile(written)
        found = {other for other, text in included.items() if other not in own and pattern.search(text)}
        return found | {
            module
            for module, text in modules.items()
            if not module.startswith(_DECLARING) and re.search(rf"[\"']{re.escape(partial)}(?:#[\w-]+)?[\"']", text)
        }

    def places(name: str) -> set[str]:
        """A page is its own place; a partial a view draws alone, as a
        fragment, is placed by the view's module."""

        if name not in seen:
            seen[name] = set()
            if name in frames or name.startswith(_RENDERED):
                seen[name] = {_EVERY}
            elif name not in owner and not Path(name).name.startswith("_"):
                seen[name] = {name}
            else:
                seen[name] = set().union(
                    *(places(drawer) if drawer in templates else {drawer} for drawer in drawers(name))
                )
        return seen[name]

    return places


def one_place_rules(templates: dict[str, str], modules: dict[str, str], sheets: dict[str, str]) -> list[str]:
    """Each rule of a part only one place draws, and each part that says it is
    a page's own drawing where that page is not its one place."""

    every = parts()
    places = _places(templates, modules, every)
    classes: dict[str, str] = {}
    shared: set[str] = set()
    faults = []
    for part in every:
        name = part.name or part.partial
        reached = set().union(*(places(file) for file in part.files())) if part.partial else {_EVERY}
        if part.drawing_of and reached != {part.drawing_of}:
            faults.append(f"{name} is {part.drawing_of}'s own drawing, but is drawn by {', '.join(sorted(reached))}")
        if part.public or len(reached) > 1 or _EVERY in reached or part.drawing_of:
            shared |= part.owned()
            continue
        label = f"{name} (drawn by {', '.join(sorted(reached)) or 'nothing'})"
        classes |= {word: label for word in part.owned() if not word.endswith("*")}
    faults += [f"{path}:{line}  {what}" for path, line, what in rules_naming(classes, sheets, frozenset(shared))]
    return faults


@tag("source", "tree")
class PartSheetTests(SimpleTestCase):
    def test_a_rule_styles_a_shared_part_never_one_only_one_page_draws(self):
        templates = {
            path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(TEMPLATES.rglob("*.html"))
        }
        modules = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for folder in ("hq", "hq_sdk")
            for path in sorted((ROOT / folder).rglob("*.py"))
            if "tests" not in path.parts and "migrations" not in path.parts
        }
        sheets = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "static" / "css").rglob("*.css"))
        }

        faults = one_place_rules(templates, modules, sheets)

        self.assertFalse(
            faults,
            "A part only one page draws has rules of its own. Draw it with the shared part that does its job, "
            "grow that part an option, or share the part (drawn in a second place, or public for an extension "
            "to write), its rules then the shared part's:\n" + "\n".join(faults),
        )
