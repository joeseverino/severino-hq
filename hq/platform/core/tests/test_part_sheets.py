"""A sheet's rules style shared parts, never a part only one page draws.

A part one page alone draws is a job a shared primitive should do: its rules
are a page styled by hand, which the next page needing the same job copies.
So every rule that names a part's class is that of a part drawn in more than
one place, or one an extension may write too (``public``). A part that is one
page's own drawing, a job no other page has, says so (``drawing_of``), and is
held to it: that page is the one place it is drawn.

A part is placed where the source can draw it, read from the templates and
views, never from a render (``look_reach``), which places only what one
catalog entry or seed's data happens to draw.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.core import included_parts
from hq.platform.core.template_contract import FRAMES, parts
from hq.platform.core.template_parts import Part
from hq.platform.core.template_source import Source
from hq.platform.core.tests.css_usage import blocks_of, named

ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = ROOT / "templates"
# Modules that declare, list or check the parts, which name a partial without drawing a page.
_DECLARING = (
    "hq/platform/core/hand_drawn.py",
    "hq/platform/core/included_actions.py",
    "hq/platform/core/included_parts.py",
    "hq/platform/core/included_queue.py",
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


def _read(folder: Path, pattern: str, within: Path = ROOT) -> dict[str, str]:
    return {
        path.relative_to(within).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob(pattern))
        if "tests" not in path.parts and "migrations" not in path.parts
    }


def one_place_rules() -> list[str]:
    """Each rule of a part only one place draws, and each part that says it is
    a page's own drawing where that page is not its one place."""

    every = parts()
    templates = _read(TEMPLATES, "*.html", TEMPLATES)
    included = {name: _EXTENDS.sub("", Source(text).text) for name, text in templates.items()}
    modules = {name: text for folder in ("hq", "hq_sdk") for name, text in _read(ROOT / folder, "*.py").items()}
    owner = {name: part for part in every for name in part.files()}
    # A module draws a part it imports by name too, as a table's piece (`tables.Piece`).
    imported = {id(value): name for name, value in vars(included_parts).items() if isinstance(value, Part)}
    seen: dict[str, set[str]] = {}

    def places(name: str) -> set[str]:
        """Each page template and view module that draws ``name``, through
        every part and partial between: a page is its own place, and a partial
        a view draws alone, as a fragment, is placed by the view's module."""

        if name in seen:
            return seen[name]
        seen[name] = set()
        part = owner.get(name)
        if name in {"base.html", *FRAMES} or name.startswith(_RENDERED):
            seen[name] = {_EVERY}
        elif not part and not Path(name).name.startswith("_"):
            seen[name] = {name}
        else:
            own = set(part.files()) if part else {name}
            quoted = rf"[\"']{re.escape(part.partial.partition('#')[0] if part else name)}(?:#[\w-]+)?[\"']"
            tagged = re.compile(quoted + (rf"|\{{%\s*{re.escape(part.name)}\b" if part and part.name else ""))
            found = {other for other, text in included.items() if other not in own and tagged.search(text)}
            if part and id(part) in imported:
                quoted += rf"|from hq\.platform\.core\.included_\w+ import [^\n]*\b{imported[id(part)]}\b"
            seen[name] = set().union(*(places(other) for other in found)) | {
                module
                for module, text in modules.items()
                if not module.startswith(_DECLARING) and re.search(quoted, text)
            }
        return seen[name]

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
    for path, text in _read(ROOT / "static" / "css", "*.css").items():
        for block in blocks_of(text):
            names = set(named(block.prelude))
            if (
                block.style
                and names.isdisjoint(shared)
                and (hit := sorted({classes[n] for n in names & classes.keys()}))
            ):
                faults.append(f"{path}:{block.line}  {', '.join(hit)}: {block.prelude}")
    return faults


@tag("source", "tree")
class PartSheetTests(SimpleTestCase):
    def test_a_rule_styles_a_shared_part_never_one_only_one_page_draws(self):
        faults = one_place_rules()

        self.assertFalse(
            faults,
            "A part only one page draws has rules of its own. Draw it with the shared part that does its job, "
            "grow that part an option, or share the part (drawn in a second place, or public for an extension "
            "to write), its rules then the shared part's:\n" + "\n".join(faults),
        )
