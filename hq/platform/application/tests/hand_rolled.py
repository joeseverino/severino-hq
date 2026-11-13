"""The page-class check's reader, and the baseline of what it still holds.

A support module rather than part of the check, so ``tools/audit_hand_rolled.py``
reads the same pairs, and ``mise run test --at`` holds a change to the reader
to the check at a ref.
"""

import re

from django.test.signals import template_rendered

from hq.platform.core.interface_text import TEMPLATE_COMMENT
from hq.platform.core.stylesheets import STATIC
from hq.platform.core.table_cells import ROLE_CLASSES
from hq_sdk.template_contract import CLASSES
lazy from hq.platform.core.tests.browser.catalog import CATALOG

from .test_style_usage import TEMPLATE_PREFIX

# The host's own templates, which no extension's check reads.
TEMPLATES = STATIC.parent / "templates"
# One template and class a line, sorted, so two branches that each drop a pair
# merge cleanly, and a conflict between them still reads as both sides' pairs.
BASELINE = STATIC.parent / "tools" / "lint" / "hand-rolled.txt"
_HEADER = (
    "# The classes each host template draws that no shared part declares yet, one\n"
    "# template and class a line. mise run fix drops a pair once its page draws\n"
    "# from the shared parts, and nothing adds one.\n"
)
_CONFLICT = re.compile(r"^(?:<{7}|\|{7}|={7}|>{7})(?:\s|$)")

_INTERPOLATED = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
# A variable is decided at render time, so a word joined to one is not a
# literal class. A tag only chooses which words are written, so it ends the
# word before it and starts the one after, unless a hyphen joins the word
# across it (``tone-{% if a %}ok{% endif %}``), which names a built class.
_VARIABLE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_TAG = re.compile(r"\{%.*?%\}", re.DOTALL)


def _literal_words(value: str) -> set[str]:
    words = set()
    for word in value.split():
        pieces = word.split("\1")
        joined = any(piece.startswith("-") for piece in pieces[1:]) or any(piece.endswith("-") for piece in pieces[:-1])
        if "\0" in word or joined:
            continue
        words.update(piece for piece in pieces if piece)
    return words


def literal_classes(text: str) -> set[str]:
    """Every class a template names in full, outside its comments: in a class
    attribute, or as the css= a tag draws as one."""

    text = TEMPLATE_COMMENT.sub("", text)
    options = [css for tag in _INTERPOLATED.findall(text) for css in re.findall(r'\bcss="([^"]*)"', tag)]
    attributes = re.findall(r'\bclass="([^"]*)"', _TAG.sub("\1", _VARIABLE.sub("\0", text)))
    return {word for value in (*options, *attributes) for word in _literal_words(value)}


def built_prefixes(text: str) -> set[str]:
    """Each class a template builds at render time, as its prefix and ``*``:
    ``is-{{ state }}`` is ``is-*``."""

    text = re.sub(r"\{%.*?%\}", " ", re.sub(r"\{\{.*?\}\}", "{{}}", TEMPLATE_COMMENT.sub("", text), flags=re.DOTALL))
    attributes = re.findall(r'\bclass="([^"]*)"', text, flags=re.DOTALL)
    return {f"{prefix}*" for value in attributes for prefix in TEMPLATE_PREFIX.findall(value)}


def _drawn_by_the_catalog() -> dict[str, set[str]]:
    """The templates the component catalog draws, each with what of it is
    drawn: its own name for the whole file, else the ``{% partialdef %}``
    names drawn on their own."""

    drawn: dict[str, set[str]] = {}

    def record(sender, template, **kwargs):
        if template.origin and template.origin.template_name:
            drawn.setdefault(template.origin.template_name, set()).add(template.name)

    template_rendered.connect(record)
    try:
        for name, entry in CATALOG.items():
            entry.drawn(name)
    finally:
        template_rendered.disconnect(record)
    return drawn


def _fragment(text: str, name: str) -> re.Match[str] | None:
    return re.search(rf"\{{%\s*partialdef\s+{re.escape(name)}\b.*?\{{%\s*endpartialdef\b.*?%\}}", text, re.DOTALL)


def hand_rolled_classes() -> set[tuple[str, str]]:
    """Each class a host template draws that no shared part declares, with
    the template's name.

    A shared class is one ``hq_sdk``'s CLASSES publishes, a table role the
    region reads, or one a part the component catalog draws writes or builds.
    A part is declared by its catalog entry, so a page's markup moved into a
    partial with none is still the page's own: its classes count here."""

    drawn = _drawn_by_the_catalog()
    shared = set(CLASSES) | set(ROLE_CLASSES)
    pages = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        name, text = path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")
        whole = name in drawn.get(name, ())
        fragments = [found.group() for part in drawn.get(name, ()) if (found := _fragment(text, part))]
        for markup in [text] if whole else fragments:
            shared |= literal_classes(markup) | built_prefixes(markup)
        if not whole:
            for fragment in fragments:
                text = text.replace(fragment, "")
            pages.append((name, text))
    return {
        (name, word)
        for name, text in pages
        for word in literal_classes(text) | built_prefixes(text)
        if word not in shared
    }


def held(text: str) -> set[tuple[str, str]]:
    """The pairs a baseline's text holds; a merge conflict's are both sides'."""

    pairs = set()
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or _CONFLICT.match(line):
            continue
        name, word = line.split()
        pairs.add((name, word))
    return pairs


def written(pairs: set[tuple[str, str]]) -> str:
    """The baseline's text as ``mise run fix`` writes it."""

    return _HEADER + "".join(f"{name} {word}\n" for name, word in sorted(pairs))


def lowered(text: str, found: set[tuple[str, str]]) -> str:
    """The baseline's text with every pair the reader no longer finds dropped.
    A pair found and not held is a new hand-rolled class, which the check
    refuses, so it is never added."""

    return written(held(text) & found)
