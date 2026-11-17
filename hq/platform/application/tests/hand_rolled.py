"""The page-class check's reading of the host: which templates are shared
parts, and the classes each other template writes beyond the public ones.

A support module rather than part of the check, so ``mise run test --at``
holds a change to the reader to the check at a ref.
"""

import re

from hq.platform.core.interface_text import TEMPLATE_COMMENT
from hq.platform.core.stylesheets import STATIC
from hq.platform.core.table_cells import ROLE_CLASSES
from hq_sdk.template_contract import CLASSES, PARTIALS, class_words

# The host's own templates, which no extension's check reads.
TEMPLATES = STATIC.parent / "templates"


def _named_by_code() -> str:
    """HQ's own Python outside its tests, where a tag or a view draws a part."""

    root = STATIC.parent
    return "\n".join(
        path.read_text(encoding="utf-8")
        for base in ("hq", "hq_sdk")
        for path in sorted((root / base).rglob("*.py"))
        if "tests" not in path.parts and path.name != "template_contract.py"
    )


# Where a part lives: the shared partials, and Django's form templates.
PART_FOLDERS = ("partials/", "django/forms/")
# Templates that override Django's own, which its form renderer draws by
# name for every form.
DJANGO_OWN = "django/"


# The widgets drawn once in the product, each by the one page that is its
# feature, which keep classes of their own. The list only shrinks: a widget
# that gains a second page, or is redrawn from the shared parts, leaves it,
# and a new widget is a shared part or joins it in a reviewed diff.
ONE_OF_A_KIND = {
    "partials/_topology_map.html": "the topology page's map, laid out and filtered by its own script",
    "partials/_connection_peering.html": "the connection page's peering of you, the link and HQ",
    "partials/_calendar_sources.html": "the calendar's sources, each a toggle with its color swatch",
    "projects/_repository_postures.html": "the posture page's meter of checks per repository",
    "partials/_relation_list.html": "a topology node's edges, stated from where it stands",
    "partials/_share_list.html": "analytics' ranked shares, each a bar against the largest",
    "partials/_tile_strip.html": "the dashboard glance's row of small figures",
    "partials/_identity.html": "an account's picture, name and handle on the watching page",
    "partials/_toggle_set.html": "the topology map's filter toggles",
    "partials/_linked_text.html": "a project's free text with the names HQ knows as links",
    "partials/_document_picture.html": "a receipt's scan, held in its shape before it arrives",
}


def _callers(templates: dict[str, str]) -> dict[str, set[str]]:
    """Each template, with the templates that include or extend it."""

    texts = {name: TEMPLATE_COMMENT.sub("", text) for name, text in templates.items()}
    named = re.compile(r"[\"']([\w./-]+\.html)(?:#[\w-]+)?[\"']")
    callers: dict[str, set[str]] = {name: set() for name in templates}
    for name, text in texts.items():
        for called in set(named.findall(text)) & callers.keys() - {name}:
            callers[called].add(name)
    return callers


def parts(templates: dict[str, str]) -> set[str]:
    """The shared parts among the host's templates: each in ``PART_FOLDERS``
    that ``hq_sdk`` publishes to extensions, that code draws (a tag, a
    widget, a view's fragment), or that two or more pages draw, through the
    templates that include or extend it. One drawn by a single page is that
    page's markup moved, so its classes are the page's own."""

    code = _named_by_code()
    callers = _callers(templates)
    drawn_by_code = {
        name
        for name in templates
        if name.startswith(DJANGO_OWN)
        or (
            name.startswith(PART_FOLDERS)
            and (name in PARTIALS or re.search(rf"[\"']{re.escape(name)}(?:#[\w-]+)?[\"']", code))
        )
    }

    def pages(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        # The pages that draw a template: itself when nothing includes it.
        if name in drawn_by_code:
            return {name, "code"}
        if not callers[name]:
            return {name}
        return {page for caller in callers[name] - seen for page in pages(caller, seen | {name})}

    return {name for name in templates if name.startswith(PART_FOLDERS) and len(pages(name)) >= 2}


def hand_rolled_classes() -> set[tuple[str, str]]:
    """Each class a host template draws that no shared part declares, with
    the template's name.

    A page writes only what an extension may: a class ``hq_sdk``'s CLASSES
    publishes, or a table role the region reads, unless it is one of a kind
    (``ONE_OF_A_KIND``). Every other class is a shared part's (``parts``),
    which a page draws through the part."""

    return {(name, word) for name, words in _own_classes().items() if name not in ONE_OF_A_KIND for word in words}


def needless_exemptions() -> set[str]:
    """Each ``ONE_OF_A_KIND`` entry that draws no class of its own any more,
    or names no template, and so leaves the list."""

    own = _own_classes()
    return {name for name in ONE_OF_A_KIND if not own.get(name)}


def _own_classes() -> dict[str, set[str]]:
    """Each template that is not a shared part, with the classes it draws that
    no shared part declares."""

    templates = {
        path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATES.rglob("*.html"))
    }
    shared_parts = parts(templates)
    shared = set(CLASSES) | set(ROLE_CLASSES)
    return {
        name: {word for _, word in class_words(text) if word not in shared}
        for name, text in templates.items()
        if name not in shared_parts
    }
