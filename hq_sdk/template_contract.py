"""What an extension's templates may use from the host, recorded in contract.json.

A host template is an interface as much as an ``hq_sdk`` export is: a child
template overriding a block the frame no longer defines renders nothing and
raises nothing, and a partial that stops reading a parameter ignores it the
same way. So which pieces are public is declared here, and what each one is
(the blocks a frame offers and what they are given, the parameters a partial
reads, the arguments a tag or filter takes, whether a class is still styled
and a hook still read) is derived from the host itself when ``manage.py
sdk_contract`` writes the record. ``hq_sdk.validation`` holds an extension's
templates to that record, and docs/Plugins.md's shared UI contract is written
from it.

A parameter is required when the partial reads it without first asking whether
it is there (``{% if why %}``, ``|default``); an include must pass every
required one by name rather than leave it to whatever the page has in scope.
"""

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
lazy import inspect

lazy from django.template import engines
lazy from django.template.backends.django import get_installed_libraries
lazy from django.template.base import FilterExpression, NodeList, Variable
lazy from django.template.defaulttags import CommentNode, ForNode, IfNode, WithNode
lazy from django.template.library import import_library
lazy from django.template.loader_tags import BlockNode, ExtendsNode, IncludeNode
lazy from django.template.smartif import TokenBase

lazy from .contract import _parameters

# The frames a page may extend, and the blocks of each it may fill. Every
# public block is an empty slot, so an override never needs ``block.super``.
FRAMES: dict[str, tuple[str, ...]] = {
    "page.html": ("page",),
    "list_page.html": ("summary", "row", "foot"),
    "confirm_page.html": ("consequences",),
}

FRAME_ABOUT = {
    "page.html": "Any page: the head, actions, dialogs and section navigation, drawn from `page`.",
    "list_page.html": (
        "A `TableListMixin` list: toolbar, table, empty state and pagination. "
        "`row` is one row's cells, `summary` sits above the table and is redrawn with it when a search or "
        "filter changes, `foot` is its `<tfoot>`."
    ),
    "confirm_page.html": "A destructive action's confirmation: `consequences` says what will happen.",
}

# The shared partials, each with the one line the docs say about it.
PARTIALS: dict[str, str] = {
    "partials/_activity_calendar.html": "One month of plan against execution, from `hq_sdk.ui.ActivityCalendar`.",
    "partials/_ask.html": "A do-this-now control and how its work stands, from `hq_sdk.outbound.ask`.",
    "partials/_attention_list.html": "Decisions waiting on the operator, from `hq_sdk.ui.Insight` entries.",
    "partials/_cadence.html": "Whether a thing happened, period by period, from `hq_sdk.ui.Cadence`.",
    "partials/_cadence_matrix.html": "Several cadences over the same periods, from `hq_sdk.ui.CadenceMatrix`.",
    "partials/_chart_tabs.html": "Up to four charts of one thing in the space of one.",
    "partials/_dropzone.html": "A file field that also accepts a drop.",
    "partials/_empty_state.html": "The one empty state: what is missing, then what would fill it.",
    "partials/_form_field.html": "Label, control, help text and validation errors for one form field.",
    "partials/_insight_grid.html": "Readings that need interpreting, each with its next step.",
    "partials/_job_progress.html": "A running job and what it is doing.",
    "partials/_kpi_grid.html": "Responsive linked or static figures.",
    "partials/_line_chart.html": "A measure over time on an axis fitted to it.",
    "partials/_record_list.html": "A compact list of records, from `hq_sdk.ui.ListRow`.",
    "partials/_stacked_bar_chart.html": "Quantities that add up, from `hq_sdk.ui.StackedBarChart`.",
    "partials/_timeline.html": "Chronological linked events, from `hq_sdk.ui.Timeline`.",
    "partials/_week_plan.html": "Which days of the week a thing is scheduled for, from `hq_sdk.ui.WeekPlan`.",
}

# The host's own tags and filters an extension may use. Django's are public
# too; they are recorded from Django itself.
TAGS = (
    "empty_value",
    "entity",
    "post_button",
    "reference",
    "referenced_by",
    "row_toggle",
    "table_heading",
    "table_region",
)
FILTERS = ("ago", "counted", "money", "or_empty", "span", "when", "web_url")

# Django's own libraries an extension may ``{% load %}`` beside its own.
LIBRARIES = ("humanize", "static")

# Classes an extension may write. Layout, tokens and states belong to the host;
# a class missing here is the host's to add, never the extension's to invent.
CLASSES = (
    "actions-col",
    "allocation",
    "allocation-legend",
    "btn",
    "card",
    "card-columns",
    "chart-axis",
    "chart-card",
    "chart-data",
    "chart-legend",
    "compact",
    "compact-table",
    "count",
    "danger",
    "data-table",
    "date-col",
    "domain-charts",
    "domain-head",
    "domain-head-copy",
    "domain-stack",
    "errors",
    "eyebrow",
    "field",
    "field-row",
    "figure-hero",
    "figure-hero-note",
    "figure-stats",
    "finding",
    "finding-evidence",
    "form",
    "form-actions",
    "ghost",
    "help",
    "inline",
    "key-col",
    "kv",
    "list-rows",
    "msg",
    "msg-error",
    "msg-warning",
    "muted",
    "narrow-more",
    "notice",
    "notice-attention",
    "num-col",
    "optional-col",
    "page-lede",
    "pie-chart",
    "pie-row",
    "pill",
    "pill-attention",
    "pill-good",
    "pill-neutral",
    "pill-serious",
    "primary",
    "print-footer",
    "print-only",
    "prose-list",
    "route-band-1",
    "route-band-2",
    "route-band-3",
    "route-band-4",
    "route-band-5",
    "route-frame",
    "route-line",
    "route-map",
    "route-marker",
    "route-scale",
    "route-swatch",
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
    "section-foot",
    "section-head",
    "section-period",
    "split",
    "stacked",
    "status-tabs",
    "subtle-link",
    "table-scroll",
    "tight-col",
    "two-col",
    "visually-hidden",
    "worth",
    "worth-attention",
    "worth-good",
    "worth-neutral",
)

# ``data-`` attributes the host's scripts act on that an extension may write.
HOOKS = (
    "data-chart",
    "data-sort",
    "data-submit-busy",
    "data-submit-label",
    "data-submit-status",
    "data-tip",
)

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
        children = set(getattr(node, "child_nodelists", ()))
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
        {name: {"parameters": _tag_parameters(tags[name])} for name in TAGS},
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
    css = ROOT / "static" / "css" / "app.css"
    return set(re.findall(r"\.([a-z][a-z0-9-]*)", css.read_text(encoding="utf-8")))


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
    """The template contract, derived from the host's templates, tags and assets."""

    engine = _engine()
    tags, filters = _host_tags(engine)
    defined = _defined_classes()
    scripts = _scripts()
    return {
        "frames": _frames(engine),
        "partials": _partials(engine),
        "tags": tags,
        "filters": filters,
        "django": _django(engine),
        "classes": _checked(CLASSES, defined.__contains__, "classes"),
        "hooks": _checked(HOOKS, lambda hook: _read_by(scripts, hook), "hooks"),
    }


def _section_drift(section: str, old: Any, new: Any) -> list[str]:
    if isinstance(old, list) and isinstance(new, list):
        return [f"+ templates.{section}: {name}" for name in sorted(set(new) - set(old))] + [
            f"- templates.{section}: {name}" for name in sorted(set(old) - set(new))
        ]
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return [f"~ templates.{section}"]
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            lines.append(f"+ templates.{section}: {name}")
        elif name not in new:
            lines.append(f"- templates.{section}: {name}")
        elif old[name] != new[name]:
            lines.append(f"~ templates.{section}: {name}: {old[name]} -> {new[name]}")
    return lines


def drift(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Each public template piece added, removed or reshaped, one line each."""

    return [
        line
        for section in sorted(set(before) | set(after))
        if before.get(section) != after.get(section)
        for line in _section_drift(section, before.get(section), after.get(section))
    ]


def _code(names: Iterable[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) or "nothing"


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
        lines.append(f"| `{name}` | {blocks} | {frame['about']} |")
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
        f"Host tags: {tags}. Host filters: {filters}. Django's own tags and filters, and "
        f"`{{% load %}}` of {_code(templates['django']['libraries'])} or the extension's own libraries.",
        "",
        f"Classes: {_code(templates['classes'])}.",
        "",
        f"Script hooks: {_code(templates['hooks'])}.",
    ]
    return "\n".join(lines) + "\n"
