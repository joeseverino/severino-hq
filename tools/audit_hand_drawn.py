"""audit:hand-drawn: what HQ's stylesheets still style by hand, the rules of
a part only one page draws, each with the shared primitive that should draw
it, read by audit:css-usage's reader of a sheet's rules. A shape a template
draws by hand is refused by hand_drawn's loaders, an element a script builds
by lint:js, and a public class nothing of the host writes by PublicClassTests.
"""

import os
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hq.platform.core.hand_drawn import Source
lazy from hq.platform.core.template_contract import FRAMES, parts
lazy from hq.platform.core.tests.css_usage import blocks_of, named

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


@dataclass(frozen=True)
class Found:
    """One class: its name, the primitive that should draw it, and each instance as (file, line, what)."""

    name: str
    primitive: str
    instances: list[tuple[str, int, str]]


def _sheets() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "static" / "css").rglob("*.css"))
    }


def rules_naming(
    classes: dict[str, str], sheets: dict[str, str], shared: frozenset[str] = frozenset()
) -> list[tuple[str, int, str]]:
    """Each style rule whose selector names one of ``classes`` and none of
    ``shared``, with what each class it names stands for."""

    found = []
    for path, text in sheets.items():
        for block in blocks_of(text):
            if not block.style or shared.intersection(names := named(block.prelude)):
                continue
            hit = sorted({classes[name] for name in names if name in classes})
            if hit:
                found.append((path, block.line, f"{', '.join(hit)}: {block.prelude}"))
    return found


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


def one_place_css(templates: dict[str, str], sheets: dict[str, str]) -> Found:
    """Each rule of a part only one page draws, which a shared primitive should
    absorb: a part no extension may write, drawn by one page template or one
    view module. A rule that names a shared part's class too is that part's."""

    every = parts()
    modules = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for folder in ("hq", "hq_sdk")
        for path in sorted((ROOT / folder).rglob("*.py"))
        if "tests" not in path.parts and "migrations" not in path.parts
    }
    places = _places(templates, modules, every)
    classes: dict[str, str] = {}
    shared: set[str] = set()
    for part in every:
        reached = set().union(*(places(name) for name in part.files())) if part.partial else {_EVERY}
        if part.public or len(reached) > 1 or _EVERY in reached:
            shared |= part.owned()
            continue
        label = f"{part.partial} (drawn by {', '.join(sorted(reached)) or 'nothing'})"
        classes |= {word: label for word in part.owned() if not word.endswith("*")}
    found = rules_naming(classes, sheets, frozenset(shared))
    return Found("one-place-css", "a shared primitive's own rules, or the part shared", found)


def report(found: list[Found]) -> Iterator[str]:
    for each in found:
        yield f"{len(each.instances):>4}  {each.name}: {each.primitive}"
        yield from (f"        {path}:{line}  {what}" for path, line, what in each.instances)
    yield f"{sum(len(each.instances) for each in found):>4}  in all"


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    import django

    django.setup()
    templates = {
        path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATES.rglob("*.html"))
    }
    sheets = _sheets()
    found = [one_place_css(templates, sheets)]
    print("\n".join(report(found)))


if __name__ == "__main__":
    main()
