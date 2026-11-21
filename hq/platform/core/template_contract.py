"""What an extension's templates may use from the host.

A host template is an interface as much as an export is: an override of a block
the frame no longer defines, or a parameter a partial stopped reading, renders
and raises nothing. So the public pieces are declared here, what each one is is
derived from the host's templates and tags as it is asked for, and
``plugin_validation`` holds an extension's templates to it.
"""

import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
lazy import inspect
lazy from annotationlib import Format

lazy from django.template import engines
lazy from django.template.backends.django import get_installed_libraries
lazy from django.template.base import FilterExpression, NodeList, Variable
lazy from django.template.defaulttags import CommentNode, ForNode, IfNode, WithNode
lazy from django.template.library import import_library
lazy from django.template.loader_tags import BlockNode, ExtendsNode, IncludeNode
lazy from django.template.smartif import TokenBase

lazy from hq.platform.core.hand_drawn import SHAPES
lazy from hq.platform.core.included_parts import PARTS
lazy from hq.platform.core.interface_text import TEMPLATE_COMMENT
lazy from hq.platform.core.stylesheets import stylesheet
lazy from hq.platform.core.table_cells import ROLE_CLASSES
lazy from hq.platform.core.template_parts import Part
lazy from hq.platform.core.templatetags.value_tags import PresentNode

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
        "A `ListPageMixin` list: toolbar, table, empty state and pagination. "
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

# The host's own filters an extension may use. Its tags are each its ``Part``
# declares public, and Django's are public too, recorded from Django itself.
FILTERS = (
    "breakable",
    "counted",
    "measured",
    "money",
    "or_empty",
    "outside",
    "queue_entries",
    "readout",
    "record",
    "span",
    "when",
    "when_text",
    "web_url",
)

# Django's own libraries an extension may ``{% load %}`` beside its own.
LIBRARIES = ("static",)

# The layout classes no part owns, which any template may write; with each
# part's ``public_classes``, the classes an extension may write. A class
# missing from both is the host's to add, never the extension's to invent.
CLASSES = (
    "actions-col",
    "cell-data",
    "cell-part",
    "cell-whole",
    "compact",
    "count",
    "danger",
    "data-table",
    "date-col",
    "eyebrow",
    "ghost",
    "glyph",
    "help",
    "inline",
    "key-col",
    "keyboard-only",
    "narrow-more",
    "narrow-only",
    "nowrap",
    "one-line",
    "optional-col",
    "page-lede",
    "primary",
    "prose",
    "row-below",
    "row-gutter",
    "row-more",
    "section-foot",
    "subtle-link",
    "swatch",
    "swatch-key",
    "table-scroll",
    "tight-col",
    "tone-attention",
    "tone-good",
    "tone-neutral",
    "tone-serious",
    "visually-hidden",
    "whole",
    "wide-only",
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
    (``tone-{{ tone }}``, named ``tone-*``). A tag only chooses which words
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


# Read by every template through the host's context processors and Django's
# own; never a parameter a page passes.
_AMBIENT = frozenset({"block", "csrf_token", "perms", "request", "user"})
# Words Django's ``{% if %}`` reads as values rather than variables.
_LITERALS = frozenset({"None", "True", "False"})
_DEFAULTING = frozenset({"default", "default_if_none"})

ROOT = Path(__file__).resolve().parents[3]


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

    def walk(self, nodelist: Iterable[Any], bound: set[str], guards: frozenset[str]) -> set[str]:
        """Note what the nodes read, and answer what is bound after them."""

        bound = set(bound)
        for node in nodelist:
            bound |= self._node(node, bound, guards)
            for binding in ("target_var", "asvar", "var_name"):
                name = getattr(node, binding, None)
                if isinstance(name, str) and name:
                    bound.add(name)
        return bound

    def _node(self, node: Any, bound: set[str], guards: frozenset[str]) -> set[str]:
        """Note what the node reads, and answer what it binds for the nodes after it."""

        if isinstance(node, CommentNode):
            return set()
        if isinstance(node, ForNode):
            self.note(node.sequence, bound, guards)
            self.walk(node.nodelist_loop, bound | set(node.loopvars) | {"forloop"}, guards)
            self.walk(node.nodelist_empty, bound, guards)
        elif isinstance(node, IfNode):
            branches = []
            for condition, branch in node.conditions_nodelists:
                names = frozenset(name for name, _ in _expression_names(condition))
                self.note(condition, bound, guards, guarded=True)
                branches.append(self.walk(branch, bound, guards | names))
            # What every branch binds, an else among them, is bound after it.
            if node.conditions_nodelists[-1][0] is None:
                return set.intersection(*branches)
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
        return set()

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
    for part in sorted((part for part in parts() if part.public and not part.name), key=lambda part: part.partial):
        name, about = part.partial, part.about
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


def _parameters(target: Any) -> list[str] | None:
    """A signature as short strings: ``name``, ``name=`` when it has a default,
    ``*args``/``**kwargs``, and the bare ``*`` and ``/`` markers Python uses."""

    try:
        signature = inspect.signature(target, annotation_format=Format.STRING)
    except TypeError, ValueError:
        # A callable Python gives no signature (a builtin's) has none to write.
        return None
    parameters = list(signature.parameters.values())
    rendered: list[str] = []
    keyword_only_started = False
    for index, parameter in enumerate(parameters):
        kind = parameter.kind
        if kind is inspect.Parameter.KEYWORD_ONLY and not keyword_only_started:
            rendered.append("*")
            keyword_only_started = True
        if kind is inspect.Parameter.VAR_POSITIONAL:
            rendered.append(f"*{parameter.name}")
            keyword_only_started = True
            continue
        if kind is inspect.Parameter.VAR_KEYWORD:
            rendered.append(f"**{parameter.name}")
            continue
        optional = parameter.default is not inspect.Parameter.empty
        rendered.append(f"{parameter.name}=" if optional else parameter.name)
        following = parameters[index + 1] if index + 1 < len(parameters) else None
        if kind is inspect.Parameter.POSITIONAL_ONLY and (following is None or following.kind is not kind):
            rendered.append("/")
    return rendered


def _tag_parameters(compile_function: Any) -> list[str]:
    target = getattr(compile_function, "__wrapped__", None)
    if target is None:
        return []
    parameters = _parameters(target) or []
    parameters = parameters[1:] if parameters[:1] == ["context"] else parameters
    # A block tag is handed what it encloses as ``content``, never by the page.
    return parameters[1:] if parameters[:1] == ["content"] else parameters


def _part(compile_function: Any) -> Part | None:
    target = getattr(compile_function, "__wrapped__", None)
    return target if isinstance(target, Part) else None


def undeclared(engine: Any, every: Iterable[Part]) -> list[str]:
    """Each host tag that is no part and answers no value (``answers``), and
    each of the host's partials no part draws: a piece of markup drawn by
    neither is one no rule, option or example holds."""

    tags = (
        f"{{% {name} %}}"
        for path, library in zip(engine.builtins, engine.template_builtins, strict=True)
        if path.startswith("hq.")
        for name, compile_function in library.tags.items()
        if not (_part(compile_function) or getattr(compile_function, "answers", False))
    )
    files = {name for part in every for name in part.files()}
    partials = (
        name
        for path in sorted((ROOT / "templates" / "partials").glob("*.html"))
        if (name := f"partials/{path.name}") not in files
    )
    return [*tags, *partials]


def parts() -> tuple[Part, ...]:
    """Every shared part: each tag's, read from what it wraps, and each a
    template includes by path. A class or a piece is one part's own, so two
    declaring it is refused here, where every reader of a part's classes reads
    them, as is a host tag or partial that is no part's (``undeclared``)."""

    engine = _engine()
    tags, _ = _libraries(engine)
    found = (*(part for compile_function in tags.values() if (part := _part(compile_function))), *PARTS)
    every = tuple({id(part): part for part in found}.values())
    if missing := undeclared(engine, every):
        raise ValueError(f"Every host tag and partial is a part's, or a tag `answers` a value: {', '.join(missing)}.")
    owners: dict[str, list[str]] = {}
    for part in every:
        for word in (*part.owned(), *part.pieces):
            owners.setdefault(word, []).append(part.name or part.partial)
    if shared := sorted(f"{word} ({', '.join(names)})" for word, names in owners.items() if len(names) > 1):
        raise ValueError(f"A class or a piece is one part's own, but parts share: {'; '.join(shared)}.")
    return every


def public_classes() -> set[str]:
    """The classes any template may write: the layout classes and each part's public ones."""

    return set(CLASSES).union(*(part.public_classes for part in parts()))


def _tag(compile_function: Any, part: Part) -> dict[str, Any]:
    """A public part's tag: its parameters, the markers its later parts open
    with (``{% aside %}``), which a page writes only inside that tag, and what
    its Part says of it and of each option."""

    record: dict[str, Any] = {
        "parameters": _tag_parameters(compile_function),
        "parts": list(part.parts[1:]),
        "about": part.about,
        "example": part.default().template,
        "data": part.data,
        "ordered": list(part.ordered),
        "options": {
            name: {
                "default": option.default,
                "choices": list(option.choices),
                "about": option.about,
                "required": option.required,
            }
            for name, option in (part.ordered | part.options).items()
        },
    }
    return record | ({"bare_with": part.bare_with} if part.bare_with else {})


def _libraries(engine: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    tags: dict[str, Any] = {}
    filters: dict[str, Any] = {}
    for library in engine.template_builtins:
        tags.update(library.tags)
        filters.update(library.filters)
    return tags, filters


def _host_tags(engine: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    tags, filters = _libraries(engine)
    missing = [name for name in FILTERS if name not in filters]
    if missing:
        raise ValueError(f"Public filters the host no longer has: {', '.join(missing)}.")
    public = {
        name: part for name, compile_function in tags.items() if (part := _part(compile_function)) and part.public
    }
    return (
        {name: _tag(tags[name], public[name]) for name in sorted(public)},
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


def _rules() -> dict[str, dict[str, Any]]:
    """Each part's rules by the name a page writes it by, its tag or its
    partial, and whether an extension may draw it."""

    return {
        part.name or part.partial: {"public": part.public, "rules": inspect.cleandoc(part.rules)}
        for part in parts()
        if part.rules
    }


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
        "classes": _checked(public_classes(), defined.__contains__, "classes"),
        "hooks": _checked(HOOKS, lambda hook: _read_by(scripts, hook), "hooks"),
        "styled": _checked(STYLED, lambda hook: f"[{hook}]" in sheets, "styled attributes"),
        "rules": _rules(),
    }
    plain: dict[str, Any] = json.loads(json.dumps(contract, sort_keys=True))
    return plain


def _code(names: Iterable[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) or "nothing"


def _cell(text: str) -> str:
    """Text for a Markdown table cell, its markup shown as code."""

    return re.sub(r"(\{%.*?%\}|<[a-z/][^>]*>|partials/\S+\.html)", r"`\1`", text).replace("|", "\\|")


def _literal(value: Any) -> str:
    """A default as a template writes it; nothing for one that is empty."""

    return "" if value in ("", []) else json.dumps(value) if isinstance(value, str) else str(value)


# Where an option a tag takes in order stands among them.
_ORDINALS = ("first", "second", "third")


def _tags_table(tags: dict[str, Any]) -> list[str]:
    """Each part's tag, then the options it takes."""

    lines = ["| Tag | Later parts | What it is | Written |", "| --- | --- | --- | --- |"]
    for name, tag in tags.items():
        example, about = (tag[key].replace("|", "\\|") for key in ("example", "about"))
        lines.append(f"| `{{% {name} %}}` | {_code(tag['parts'])} | {about} | `{example}` |")
    lines += ["", "| Tag | Option | Default | Choices | What it does |", "| --- | --- | --- | --- | --- |"]
    for name, tag in tags.items():
        ordered = tag["ordered"]

        # Those taken in order first, as they are written, then by name, and what the tag gathers last.
        def place(option: str, ordered: list[str] = ordered) -> tuple[int, int, str]:
            return (0, ordered.index(option), "") if option in ordered else (1 + option.startswith("*"), 0, option)

        for option in sorted(tag["options"], key=place):
            shape, default = tag["options"][option], _literal(tag["options"][option]["default"])
            said = f"`{option}`, {_ORDINALS[ordered.index(option)]} and unnamed" if option in ordered else f"`{option}`"
            written = "required" if shape["required"] else f"`{default}`" if default else ""
            choices = _code(str(choice) for choice in shape["choices"] if choice != "") if shape["choices"] else ""
            lines.append(f"| `{name}` | {said} | {written} | {choices} | {shape['about'].replace('|', '\\|')} |")
        if tag["data"]:
            lines.append(f"| `{name}` | `data_*` | | | Each a `data-` attribute. |")
    return lines


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
    lines += ["", *_tags_table(templates["tags"])]
    filters = ", ".join(
        f"`|{name}" + {"none": "", "optional": "[:arg]", "required": ":arg"}[shape["argument"]] + "`"
        for name, shape in templates["filters"].items()
    )
    lines += [
        "",
        (
            f"Host filters: {filters}. Django's own tags and filters, and "
            f"`{{% load %}}` of {_code(templates['django']['libraries'])} or the extension's own libraries."
        ),
        "",
        f"Classes: {_code(templates['classes'])}.",
        "",
        f"Script hooks: {_code(templates['hooks'])}. Styled attributes: {_code(templates['styled'])}.",
        "",
        "| Shape | Drawn by hand | Write instead |",
        "| --- | --- | --- |",
        *(f"| `{shape.name}` | {_cell(shape.said)} | {_cell(shape.instead)} |" for shape in SHAPES),
        "",
        "## Each part's rules",
    ]
    for name, rules in templates["rules"].items():
        lines += ["", f"### {name}" + ("" if rules["public"] else " (host only)"), "", rules["rules"]]
    return "\n".join(lines) + "\n"
