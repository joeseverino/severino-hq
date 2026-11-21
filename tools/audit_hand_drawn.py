"""audit:hand-drawn: what HQ's own templates and stylesheets still draw by
hand, a class to a row in the order the UI waves take them, each with the
shared primitive that should draw it. Each class is read by the reader its
wave's guard will be: a template's by a hand_drawn ``Shape``, a sheet's rules
by audit:css-usage's reader of them. An element a script builds is lint:js's
to refuse, and a public class nothing of the host writes is PublicClassTests'.
"""

import os
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hq.platform.core.hand_drawn import SHAPES, Shape, Source, tagged, within
lazy from hq.platform.core.template_contract import FRAMES, parts
lazy from hq.platform.core.tests.css_usage import blocks_of, named

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"

_EXISTING = {shape.name: shape for shape in SHAPES}
_CHOICE = r"[\s}]type=[\"']?(?:checkbox|radio)\b"
_CHIP_CLASS = r"[\s}]class=[\"'](?:[^\"']*[\s}])?(?:[\w-]*(?:chips?|badge|pill|tag)|[\w-]+-(?:count|age))(?:[\s\"'{]|$)"


def _form_controls(source: Source) -> Iterator[int]:
    """Each form, field and label outside a choice: a choice's box and its
    label are the filter-choice shape's."""

    choices = set(within("label", _CHOICE)(source))
    yield from tagged("form|textarea|fieldset")(source)
    yield from tagged("input", without=r"[\s}]type=[\"']?(?:checkbox|radio|search)\b")(source)
    yield from (at for at in tagged("label")(source) if at not in choices)


# The template classes, as shapes not yet refused. Each wave moves its own
# into hand_drawn.SHAPES, where the loaders refuse it, as the guard it adds.
COUNTED = (
    Shape(
        name="filter-choice",
        said="A choice of several drawn as boxes by hand, where the toggle set part draws it.",
        instead='{% include "partials/_toggle_set.html" %}, or {% select %} for one choice',
        find=tagged("input", _CHOICE),
        refuses=('<label><input type="checkbox" name="kind" value="a"> A</label>',),
        keeps=('<input type="hidden" name="kind" value="a">',),
        drawn_by={
            "partials/_toggle_set.html": "the toggle set part itself",
            "partials/_row_select.html": "the table's box that selects its row",
            "partials/_table_heading.html": "the table's box that selects every row",
            "partials/_chart_tabs.html": "the chart tabs' own switch, which turns with no script",
        },
    ),
    Shape(
        name="form-control",
        said="A form, a field or a label drawn by hand, where the form, field and filter bar parts draw it.",
        instead='{% include "partials/_form.html" %} of a Django form, or {% filter_bar %} for what a list keeps',
        find=_form_controls,
        refuses=('<form method="get"><label>Kind <input name="kind"></label></form>', '<input type="hidden" name="a">'),
        keeps=('<label><input type="checkbox" name="a"> A</label>', '<input type="search" name="q">'),
        drawn_by={
            "partials/_form.html": "the form part itself",
            "django/forms/field.html": "the field part itself",
            "django/forms/widgets/choice_cards.html": "the field part's widget",
            "django/forms/widgets/multiple_input.html": "the field part's widget",
            "partials/_filter_bar.html": "the filter bar part itself",
            "partials/_control_label.html": "the control label part itself",
            "partials/_toggle_set.html": "the toggle set part itself",
            "partials/_dropzone.html": "the dropzone part, a file field",
            "partials/_row_field.html": "the row field part, a field in a table's row",
            **_EXISTING["form"].drawn_by,
            **_EXISTING["filter-bar"].drawn_by,
        },
    ),
    Shape(
        name="chip",
        said="A chip, a badge, a count or an age drawn by hand, where the status part or a value filter draws it.",
        instead='{% status word chip=True %}, a count by {{ n|counted:"thing" }}, an age by {{ at|ago }}',
        find=tagged(r"[\w-]+", _CHIP_CLASS),
        refuses=('<span class="scope-chip">A</span>', '<span class="sweep-age">2h</span>'),
        keeps=('<span class="count">2</span>', '{% status "Online" chip=True %}'),
        drawn_by={
            "partials/_list.html": "the list part, whose chips kind draws a link as a chip",
            "partials/_reading_age.html": "the reading age part itself",
            "partials/_cadence.html": "the cadence part's tally, a line of text",
            "partials/_record_list.html": "the record list lays out the chips the status part draws",
        },
    ),
)

# Found by no class of the old inventory: a button the button tag does not
# draw, which says it is the only thing that writes one.
BUTTON = Shape(
    name="button",
    said="A button drawn by hand, where the button tag draws every control that looks like one.",
    instead="{% button %}, which takes a popover, a dialog command and each data_* hook",
    find=tagged("button"),
    refuses=('<button type="button" popovertarget="menu">Menu</button>',),
    keeps=('{% button "Menu" popovertarget="menu" %}',),
)


@dataclass(frozen=True)
class Found:
    """One class: its name, the primitive that should draw it, and each instance as (file, line, what)."""

    name: str
    primitive: str
    instances: list[tuple[str, int, str]]


def drawn(shape: Shape, templates: dict[str, str]) -> Found:
    """Each place a template outside the shape's own draws it."""

    found = []
    for name, text in templates.items():
        if name in shape.drawn_by:
            continue
        source = Source(text)
        for at in sorted(set(shape.find(source))):
            end = source.masked.find(">", at)
            found.append((f"templates/{name}", source.line(at), " ".join(source.text[at : end + 1].split())))
    return Found(shape.name, shape.instead, found)


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
    found = [
        *(drawn(shape, templates) for shape in COUNTED),
        one_place_css(templates, sheets),
        drawn(BUTTON, templates),
    ]
    print("\n".join(report(found)))


if __name__ == "__main__":
    main()
