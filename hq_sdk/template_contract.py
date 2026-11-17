"""What an extension's templates may use from the host.

A host template is an interface as much as an export is: an override of a block
the frame no longer defines, or a parameter a partial stopped reading, renders
and raises nothing. So the public pieces are declared here, what each one is is
derived from the host's templates and tags as it is asked for, and
``hq_sdk.validation`` holds an extension's templates to it.
"""

import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
lazy import inspect

# Read only by hand_drawn_rules, which development alone runs: no image installs it.
lazy import yaml
lazy from django.template import engines
lazy from django.template.backends.django import get_installed_libraries
lazy from django.template.base import FilterExpression, NodeList, Variable
lazy from django.template.defaulttags import CommentNode, ForNode, IfNode, WithNode
lazy from django.template.library import import_library
lazy from django.template.loader_tags import BlockNode, ExtendsNode, IncludeNode
lazy from django.template.smartif import TokenBase

lazy from hq.platform.core.interface_text import TEMPLATE_COMMENT
lazy from hq.platform.core.stylesheets import stylesheet
lazy from hq.platform.core.table_cells import ROLE_CLASSES
lazy from hq.platform.core.templatetags.part_tags import SectionTakes
lazy from hq.platform.core.templatetags.value_tags import PresentNode, Takes

lazy from .contract import _parameters

# The frames a page may extend, and the blocks of each it may fill. Every
# public block is an empty slot, so an override never needs ``block.super``.
FRAMES: dict[str, tuple[str, ...]] = {
    "page.html": ("page", "print_footer"),
    "list_page.html": ("summary", "row", "foot"),
    "confirm_page.html": ("consequences",),
    "form_page.html": (),
}

FRAME_ABOUT = {
    "page.html": "Any page: the head, actions, dialogs and section navigation, drawn from `page`.",
    "list_page.html": (
        "A `TableListMixin` list: toolbar, table, empty state and pagination. "
        "`row` is one row's cells, `summary` sits above the table and is redrawn with it when a search or "
        "filter changes, `foot` is its `<tfoot>`."
    ),
    "confirm_page.html": "A destructive action's confirmation: `consequences` says what will happen.",
    "form_page.html": (
        "A page that is one form, a record's: its card and `partials/_form.html`, closing with `form_verb` and "
        "`cancel_url`; `form_fields` names a template for fields beyond the form's own loop and `form_note` says "
        "what saving does, each from the view, so the page fills no block."
    ),
}

# The shared partials, each with the one line the docs say about it.
PARTIALS: dict[str, str] = {
    "partials/_activity_calendar.html": "One month of plan against execution, from `hq_sdk.ui.ActivityCalendar`.",
    "partials/_allocation.html": "Parts of one whole as one bar, from `hq_sdk.ui.AllocationBar`.",
    "partials/_ask.html": "A do-this-now control and how its work stands, from `hq_sdk.outbound.ask`.",
    "partials/_attention_list.html": "Decisions waiting on the operator, from the entries `hq_sdk.plugin.plugin_attention_items` gathers.",
    "partials/_cadence.html": "Whether a thing happened, period by period, from `hq_sdk.ui.Cadence`.",
    "partials/_cadence_matrix.html": "Several cadences over the same periods, from `hq_sdk.ui.CadenceMatrix`.",
    "partials/_chart.html": "A measure over time from `hq_sdk.ui.line_chart`, or quantities that add up from `hq_sdk.ui.stacked_bar_chart`, or a spatial path from `hq_sdk.ui.PathChart`.",
    "partials/_chart_tabs.html": "Up to four charts of one thing in the space of one.",
    "partials/_dropzone.html": "A file field that also accepts a drop.",
    "partials/_empty_state.html": "The one empty state: what is missing, then what would fill it.",
    "partials/_form.html": "A form that writes: its token, its errors, its fields or its table, and its closing row.",
    "partials/_form_actions.html": "A form's closing row: its one verb, Cancel, and what saving does.",
    "partials/_form_errors.html": "What was wrong with a submitted form as a whole, above its fields.",
    "partials/_icon.html": "One of the host's icons, by `name`, the height of the text beside it.",
    "partials/_insight_grid.html": "Readings that need interpreting, each with its next step.",
    "partials/_job_progress.html": "A running job and what it is doing.",
    "partials/_kpi_grid.html": "Responsive linked or static figures.",
    "partials/_reading_age.html": (
        "When a reading was taken, `at` the moment, in the one wording every page uses; `unread` why there is none."
    ),
    "partials/_record_list.html": "A compact list of records, from `hq_sdk.ui.ListRow`.",
    "partials/_row_field.html": "One field of a form whose fields stand one to a row of a table, in its row.",
    "partials/_timeline.html": "Chronological linked events, from `hq_sdk.ui.Timeline`.",
    "partials/_week_plan.html": "Which days of the week a thing is scheduled for, from `hq_sdk.ui.WeekPlan`.",
}

# The host's own tags and filters an extension may use. Django's are public
# too; they are recorded from Django itself.
TAGS = (
    "arrow",
    "button_row",
    "card",
    "dot",
    "dot_list",
    "empty_value",
    "entity",
    "fact",
    "facts",
    "filter_bar",
    "muted",
    "notice",
    "outside_link",
    "button",
    "present",
    "prose_list",
    "reference",
    "referenced_by",
    "row_actions",
    "row_editor",
    "row_group",
    "row_toggle",
    "section",
    "status",
    "table_heading",
    "table_region",
    "words",
    "write_token",
)
FILTERS = (
    "breakable",
    "counted",
    "measured",
    "money",
    "or_empty",
    "outside",
    "readout",
    "record",
    "span",
    "when",
    "when_text",
    "web_url",
)

# Django's own libraries an extension may ``{% load %}`` beside its own.
LIBRARIES = ("static",)

# Classes an extension may write. Layout, tokens and states belong to the host;
# a class missing here is the host's to add, never the extension's to invent.
CLASSES = (
    "actions-col",
    "address-list",
    "allocation",
    "allocation-legend",
    "cell-data",
    "cell-part",
    "cell-whole",
    "code-list",
    "compact",
    "compact-table",
    "count",
    "danger",
    "data-table",
    "date-col",
    "domain-charts",
    "errors",
    "eyebrow",
    "finding",
    "form",
    "ghost",
    "help",
    "icon-link",
    "inline",
    "key-col",
    "keyboard-only",
    "list-rows",
    "name-line",
    "narrow-more",
    "narrow-only",
    "nowrap",
    "one-line",
    "optional-col",
    "page-lede",
    "primary",
    "prose",
    "row-actions",
    "row-below",
    "row-empty",
    "row-group",
    "row-gutter",
    "row-less",
    "row-main",
    "row-meta",
    "row-more",
    "save-bar",
    "section-flow",
    "section-foot",
    "split",
    "stacked",
    "subtle-link",
    "table-clamp",
    "table-scroll",
    "tight-col",
    "two-col",
    "visually-hidden",
    "whole",
)

_VARIABLE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_BLOCK = re.compile(r"\{%.*?%\}", re.DOTALL)
_CLASS_ATTRIBUTE = re.compile(r'\bclass="([^"]*)"')
# A tag's option that hands it class names (``css``, ``link_css``).
_CLASS_OPTION = re.compile(r"""\b(?:\w+_)?css=(["'])(.*?)\1""")
_PREFIX = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*-")


def _blanked(pattern: re.Pattern[str], mark: str, text: str) -> str:
    """Each match as one mark, keeping its line breaks so lines still count."""

    return pattern.sub(lambda match: mark + "\n" * match.group().count("\n"), text)


def _named(pieces: list[str], built: bool) -> Iterator[str]:
    if not built:
        if pieces[0]:
            yield pieces[0]
    elif _PREFIX.fullmatch(pieces[0]):
        yield f"{pieces[0]}*"


def _words(value: str) -> Iterator[str]:
    """The class names in one attribute, ``\\0`` marking a variable and ``\\1`` a tag.

    A variable is decided at render time, so a word joined to one is built
    (``pill-{{ tone }}``, named ``pill-*``). A tag only chooses which words
    are written, so it ends the word before it and starts the one after,
    unless a hyphen joins the word across it (``tone-{% if a %}ok{% endif %}``),
    which is built too.
    """

    for word in value.split():
        pieces = re.split("([\0\1])", word)
        name, built = [pieces[0]], False
        for mark, piece in zip(pieces[1::2], pieces[2::2], strict=True):
            if mark == "\0" or name[-1].endswith("-") or piece.startswith("-"):
                name.append(piece)
                built = True
            else:
                yield from _named(name, built)
                name, built = [piece], False
        yield from _named(name, built)


def class_words(text: str) -> list[tuple[int, str]]:
    """``(line, name)`` for each class a template writes outside its comments:
    in a class attribute, or as a tag's ``css`` option. A class built at
    render time is named by its prefix and ``*``.

    The one reader of a template's classes, for the host's pages and an
    extension's alike, so what one is refused the other is too.
    """

    text = _blanked(TEMPLATE_COMMENT, "", text)
    found = [
        (text.count("\n", 0, tag.start()) + 1, word)
        for tag in _BLOCK.finditer(text)
        for option in _CLASS_OPTION.finditer(tag.group())
        for word in option.group(2).split()
    ]
    markup = _blanked(_BLOCK, "\1", _blanked(_VARIABLE, "\0", text))
    found += [
        (markup.count("\n", 0, attribute.start()) + 1, word)
        for attribute in _CLASS_ATTRIBUTE.finditer(markup)
        for word in _words(attribute.group(1))
    ]
    return found


# ``data-`` attributes the host's scripts act on that an extension may write.
HOOKS = (
    "data-sort",
    "data-submit-busy",
    "data-submit-label",
    "data-submit-status",
    "data-tip",
)

# ``data-`` attributes the host's stylesheets act on that an extension may
# write: ``data-chart`` marks a chart, whose region scrolls sideways with its
# heading held (tables.css); on a chart drawn outside a region it changes
# nothing.
STYLED = ("data-chart",)


# The value tags that write a figure together with its own words, each joined
# where a line could break inside it (value_tags, through line_breaks.held).
# The hand-drawn-split-figure rule leaves each alone (test_template_values).
WORDED_FIGURES = {
    "bytes": "a byte count",
    "counted": "a count and the words for it",
    "expiry": "a date and a count of days",
    "measured": "a figure and its unit",
    "money": "an amount",
    "seconds": "a run time",
    "signed_in": "an age in words",
    "span": "a length of time",
    "when_text": "a moment in words",
}

# Read by every template through the host's context processors and Django's
# own; never a parameter a page passes.
_AMBIENT = frozenset({"block", "csrf_token", "perms", "request", "user"})
# Words Django's ``{% if %}`` reads as values rather than variables.
_LITERALS = frozenset({"None", "True", "False"})
_DEFAULTING = frozenset({"default", "default_if_none"})

ROOT = Path(__file__).resolve().parents[1]


def _filtered_names(value: Any) -> Iterator[tuple[str, bool]]:
    """What ``{{ value|filter:argument }}`` reads: the value, then each variable argument."""

    defaulted = any(getattr(function, "__name__", "") in _DEFAULTING for function, _ in value.filters)
    if isinstance(value.var, Variable) and value.var.lookups:
        yield value.var.lookups[0], defaulted
    for _, arguments in value.filters:
        yield from (
            (argument.lookups[0], False)
            for is_variable, argument in arguments
            if is_variable and isinstance(argument, Variable) and argument.lookups
        )


def _expression_names(value: Any) -> Iterator[tuple[str, bool]]:
    """Each variable an expression reads, and whether it falls back to a default."""

    if isinstance(value, FilterExpression):
        yield from _filtered_names(value)
    elif isinstance(value, Variable) and value.lookups:
        yield value.lookups[0], False
    elif isinstance(value, TokenBase):
        parts = (getattr(value, part) for part in ("value", "first", "second") if hasattr(value, part))
        for part in parts:
            yield from _expression_names(part)
    elif isinstance(value, (list, tuple, dict)):
        for item in value.values() if isinstance(value, dict) else value:
            yield from _expression_names(item)


class _Reads:
    """The variables a template reads that it does not bind itself."""

    def __init__(self, engine: Any, *, descend: bool = True) -> None:
        self.engine = engine
        # Whether an included template's reads count as this one's.
        self.descend = descend
        self.found: dict[str, bool] = {}
        self.blocks: dict[str, tuple[str, ...]] = {}
        self._including: list[str] = []

    def note(self, value: Any, bound: set[str], guards: frozenset[str], *, guarded: bool = False) -> None:
        for name, defaulted in _expression_names(value):
            if name in bound or name in _AMBIENT or name in _LITERALS:
                continue
            optional = guarded or defaulted or name in guards
            self.found[name] = self.found.get(name, False) or not optional

    def walk(self, nodelist: Iterable[Any], bound: set[str], guards: frozenset[str]) -> None:
        bound = set(bound)
        for node in nodelist:
            self._node(node, bound, guards)
            for binding in ("target_var", "asvar", "var_name"):
                name = getattr(node, binding, None)
                if isinstance(name, str) and name:
                    bound.add(name)

    def _node(self, node: Any, bound: set[str], guards: frozenset[str]) -> None:
        if isinstance(node, CommentNode):
            return
        if isinstance(node, ForNode):
            self.note(node.sequence, bound, guards)
            self.walk(node.nodelist_loop, bound | set(node.loopvars) | {"forloop"}, guards)
            self.walk(node.nodelist_empty, bound, guards)
        elif isinstance(node, IfNode):
            for condition, branch in node.conditions_nodelists:
                names = frozenset(name for name, _ in _expression_names(condition))
                self.note(condition, bound, guards, guarded=True)
                self.walk(branch, bound, guards | names)
        elif isinstance(node, PresentNode):
            # What its body reads of the value is read only when it is there.
            names = frozenset(name for name, _ in _expression_names(node.value))
            self.note(node.value, bound, guards, guarded=True)
            self.walk(node.nodelist, bound, guards | names)
        elif isinstance(node, WithNode):
            self.note(node.extra_context, bound, guards)
            self.walk(node.nodelist, bound | set(node.extra_context), guards)
        elif isinstance(node, IncludeNode):
            self._include(node, bound, guards)
        elif isinstance(node, BlockNode):
            self.blocks.setdefault(node.name, tuple(sorted(bound)))
            self.walk(node.nodelist, bound, guards)
        elif isinstance(node, ExtendsNode):
            self.walk(node.nodelist, bound, guards)
        else:
            self._generic(node, bound, guards)

    def _include(self, node: Any, bound: set[str], guards: frozenset[str]) -> None:
        self.note(node.template, bound, guards)
        self.note(node.extra_context, bound, guards)
        name = node.template.var
        if not self.descend or not isinstance(name, str) or name in self._including:
            return
        given = set(node.extra_context)
        inner = _Reads(self.engine)
        inner._including = [*self._including, name]
        inner.walk(self.engine.get_template(name).nodelist, given, frozenset())
        if node.isolated_context:
            return
        # What the included template needs and is not given comes from here.
        # What it reads only if present is its own option, not this one's.
        for read, required in inner.found.items():
            if required and read not in bound:
                self.found[read] = self.found.get(read, False) or read not in guards

    def _generic(self, node: Any, bound: set[str], guards: frozenset[str]) -> None:
        children = set(node.child_nodelists)
        for attribute, value in vars(node).items():
            if attribute in children or isinstance(value, NodeList):
                continue
            self.note(value, bound, guards)
        for attribute in children:
            nodelist = getattr(node, attribute, None)
            if nodelist:
                self.walk(nodelist, bound, guards)


def _engine() -> Any:
    return engines["django"].engine  # type: ignore[attr-defined]  # the Django backend's own engine


def _reads(engine: Any, name: str, *, descend: bool = True) -> _Reads:
    reads = _Reads(engine, descend=descend)
    reads.walk(engine.get_template(name).nodelist, set(), frozenset())
    return reads


def _frames(engine: Any) -> dict[str, Any]:
    frames = {}
    for name, blocks in FRAMES.items():
        reads = _reads(engine, name, descend=False)
        missing = [block for block in blocks if block not in reads.blocks]
        if missing:
            raise ValueError(f"{name} declares public blocks it no longer has: {', '.join(missing)}.")
        frames[name] = {
            "about": FRAME_ABOUT[name],
            "blocks": {block: {"given": sorted(reads.blocks[block])} for block in blocks},
            "reads": sorted(reads.found),
        }
    return frames


def _partials(engine: Any) -> dict[str, Any]:
    partials = {}
    for name, about in PARTIALS.items():
        found = _reads(engine, name).found
        # Required is read without first asking whether it is there ({% if why %},
        # |default), so an include passes it by name rather than leave it to
        # whatever the page has in scope.
        partials[name] = {
            "about": about,
            "required": sorted(read for read, required in found.items() if required),
            "optional": sorted(read for read, required in found.items() if not required),
        }
    return partials


def _unwrapped(function: Any) -> Any:
    return inspect.unwrap(function)


def _filter_argument(function: Any) -> str:
    parameters = list(inspect.signature(_unwrapped(function)).parameters.values())[1:]
    if not parameters:
        return "none"
    return "optional" if parameters[0].default is not inspect.Parameter.empty else "required"


def _tag_parameters(compile_function: Any) -> list[str]:
    target = getattr(compile_function, "__wrapped__", None)
    if target is None:
        return []
    parameters = _parameters(target) or []
    parameters = parameters[1:] if parameters[:1] == ["context"] else parameters
    # A block tag is handed what it encloses as ``content``, never by the page.
    return parameters[1:] if parameters[:1] == ["content"] else parameters


def _tag_parts(compile_function: Any) -> list[str]:
    """The markers a block tag's later parts open with (``{% aside %}``), which
    a page writes only inside that tag. The first part follows the opening tag."""

    target = getattr(compile_function, "__wrapped__", None)
    return list(target.parts[1:]) if isinstance(target, (Takes, SectionTakes)) else []


def _tag(compile_function: Any) -> dict[str, Any]:
    record = {"parameters": _tag_parameters(compile_function), "parts": _tag_parts(compile_function)}
    target = getattr(compile_function, "__wrapped__", None)
    bare_with = target.bare_with if isinstance(target, Takes) else ""
    return record | ({"bare_with": bare_with} if bare_with else {})


def _libraries(engine: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    tags: dict[str, Any] = {}
    filters: dict[str, Any] = {}
    for library in engine.template_builtins:
        tags.update(library.tags)
        filters.update(library.filters)
    return tags, filters


def _host_tags(engine: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    tags, filters = _libraries(engine)
    missing = [name for name in TAGS if name not in tags] + [name for name in FILTERS if name not in filters]
    if missing:
        raise ValueError(f"Public tags or filters the host no longer has: {', '.join(missing)}.")
    return (
        {name: _tag(tags[name]) for name in TAGS},
        {name: {"argument": _filter_argument(filters[name])} for name in FILTERS},
    )


def _django(engine: Any) -> dict[str, Any]:
    """Django's own tags and filters, and those of the libraries an extension may load."""

    tags: set[str] = set()
    filters: set[str] = set()
    for path, library in zip(engine.builtins, engine.template_builtins, strict=True):
        if path.startswith("django."):
            tags.update(library.tags)
            filters.update(library.filters)
    installed = get_installed_libraries()
    loadable = {}
    for name in LIBRARIES:
        library = import_library(installed[name])
        loadable[name] = sorted(set(library.tags) | set(library.filters))
    return {"tags": sorted(tags), "filters": sorted(filters), "libraries": loadable}


def _defined_classes() -> set[str]:
    # A class that declares a part's role in a table is read by the region,
    # which marks the part for the stylesheet (table_tags.mark_cells).
    return set(re.findall(r"\.([a-z][a-z0-9-]*)", stylesheet())) | set(ROLE_CLASSES)


def _scripts() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "static" / "js").rglob("*.js")))


def _read_by(scripts: str, hook: str) -> bool:
    """Whether a script reads the attribute by name or through ``dataset``."""

    head, *rest = hook.removeprefix("data-").split("-")
    return hook in scripts or f"dataset.{head}{''.join(part.title() for part in rest)}" in scripts


def _checked(names: Iterable[str], present: Any, what: str) -> list[str]:
    absent = [name for name in names if not present(name)]
    if absent:
        raise ValueError(f"Public {what} the host no longer has: {', '.join(absent)}.")
    return sorted(names)


def describe() -> dict[str, Any]:
    """The template contract, derived from the host's templates, tags and assets,
    as plain JSON data in name order, so docs/Plugins.md's tables keep one order."""

    engine = _engine()
    tags, filters = _host_tags(engine)
    defined = _defined_classes()
    scripts = _scripts()
    sheets = stylesheet()
    contract = {
        "frames": _frames(engine),
        "partials": _partials(engine),
        "tags": tags,
        "filters": filters,
        "django": _django(engine),
        "classes": _checked(CLASSES, defined.__contains__, "classes"),
        "hooks": _checked(HOOKS, lambda hook: _read_by(scripts, hook), "hooks"),
        "styled": _checked(STYLED, lambda hook: f"[{hook}]" in sheets, "styled attributes"),
    }
    plain: dict[str, Any] = json.loads(json.dumps(contract, sort_keys=True))
    return plain


def _code(names: Iterable[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) or "nothing"


def _cell(text: str) -> str:
    """Text for a Markdown table cell, its markup shown as code."""

    return re.sub(r"(\{%.*?%\}|<[a-z/][^>]*>|partials/\S+\.html)", r"`\1`", text).replace("|", "\\|")


def hand_drawn_rules() -> list[dict[str, str]]:
    """Each rule that refuses a shape a shared part draws, drawn by hand in a
    template (``tools/lint/ast-grep/rules/hand-drawn-*.yml``), which the host's
    lint and an extension's ``plugin:check`` both run."""

    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "tools/lint/ast-grep/rules").glob("hand-drawn-*.yml"))
    ]


def markdown(templates: dict[str, Any]) -> str:
    """The shared UI contract as docs/Plugins.md states it."""

    lines = [
        "| Frame | Blocks a page fills | What it is |",
        "| --- | --- | --- |",
    ]
    for name, frame in templates["frames"].items():
        blocks = ", ".join(
            f"`{block}`" + (f" (given {_code(shape['given'])})" if shape["given"] else "")
            for block, shape in frame["blocks"].items()
        )
        lines.append(f"| `{name}` | {blocks or 'nothing'} | {frame['about']} |")
    lines += [
        "",
        "| Partial | Required | Optional | What it is |",
        "| --- | --- | --- | --- |",
    ]
    for name, partial in templates["partials"].items():
        lines.append(f"| `{name}` | {_code(partial['required'])} | {_code(partial['optional'])} | {partial['about']} |")
    tags = ", ".join(f"`{{% {name} %}}`" for name in templates["tags"])
    filters = ", ".join(
        f"`|{name}" + {"none": "", "optional": "[:arg]", "required": ":arg"}[shape["argument"]] + "`"
        for name, shape in templates["filters"].items()
    )
    lines += [
        "",
        (
            f"Host tags: {tags}. Host filters: {filters}. Django's own tags and filters, and "
            f"`{{% load %}}` of {_code(templates['django']['libraries'])} or the extension's own libraries."
        ),
        "",
        f"Classes: {_code(templates['classes'])}.",
        "",
        f"Script hooks: {_code(templates['hooks'])}. Styled attributes: {_code(templates['styled'])}.",
        "",
        "| Rule | Drawn by hand | Write instead |",
        "| --- | --- | --- |",
        *(f"| `{rule['id']}` | {_cell(rule['message'])} | {_cell(rule['note'])} |" for rule in hand_drawn_rules()),
    ]
    return "\n".join(lines) + "\n"
