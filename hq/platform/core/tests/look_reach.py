"""The component catalog's entries a change to a sheet or a template reaches:
what a look names ``catalog:<path>`` for, and what fast judges in a browser
after such a change (mise-tasks/fast/look).

An entry is reached by a template its page draws, as Django's test rendering
announces each, and by a changed rule of a sheet whose classes or data
attributes its markup holds. A rule with an alternative that names neither (an
element, ``:root``) reaches every entry, as does an at-rule that holds
declarations rather than rules (``@font-face``, ``@property``,
``@keyframes``), and a statement outside every rule (``@layer a, b;``). A rule
is changed where a line of it, comments aside, differs from the base the
look's environment names (``BASE``, which fast sets to its own; else HEAD), on
either side, so a rule a change deletes reaches the entries it styled. A file
the base holds as it is is read whole.
"""

import functools
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.test.signals import template_rendered
from django.utils.text import Truncator

from hq.platform.core.stylesheets import SHEETS
from hq.platform.core.tests.css_usage import Block, blocks_of, named
from hq.platform.core.tests.task import ROOT
lazy from hq.platform.core.tests.browser import catalog

# The variable that names the ref a change is read from.
BASE = "HQ_FAST_BASE"
# The at-rules that group rules, each of which is drawn only as its rules are.
GROUPS = ("@media", "@supports", "@container", "@layer", "@scope", "@starting-style")
# A sheet's path from the checkout's root: the stylesheets live under static/.
STATIC = "static/"
# The words of a page's markup a selector can name: each class, and each data attribute.
_CLASSES = re.compile(r"""\sclass\s*=\s*["']([^"']*)["']""")
_DATA = re.compile(r"\s(data-[\w-]+)")
# A hunk of `git diff -U0`: where it starts and how many lines it spans, before and after.
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)
# What a `:not()` names is what the rule leaves out, never what it reaches.
_NOT = re.compile(r":not\([^()]*\)")


@dataclass(frozen=True, slots=True)
class Drawing:
    """What an entry's page drew: every template, and the words of its markup."""

    templates: frozenset[str]
    words: frozenset[str]


@dataclass(frozen=True, slots=True)
class Reach:
    """The entries a path reaches, in the catalog's order, and the line that says why."""

    entries: tuple[str, ...]
    said: str


@functools.cache
def drawings() -> dict[str, Drawing]:
    """Each page of the catalog as its gate draws it. The gallery holds every
    part in a shadow root of its own, which no probe reads, so it is none of
    these."""

    found = {}
    heard: set[str] = set()

    def hear(sender: Any, template: Any, **_: Any) -> None:
        if template.name:
            heard.add(template.name)

    template_rendered.connect(hear)
    try:
        for name in catalog.PAGES:
            if name == catalog.GALLERY:
                continue
            heard.clear()
            markup = catalog.page(name)
            words = {word for value in _CLASSES.findall(markup) for word in value.split()}
            found[name] = Drawing(frozenset(heard), frozenset(words.union(_DATA.findall(markup))))
    finally:
        template_rendered.disconnect(hear)
    return found


def reach(path: str) -> Reach:
    """The entries ``path`` reaches, a sheet HQ links or a template a loader
    finds; AssertionError for any other file, which no page draws."""

    if path.endswith(".css"):
        if path.removeprefix(STATIC) not in SHEETS:
            return Reach((), f"{path}: no page of the catalog links it")
        return _sheet(path)
    parts = Path(path).parts
    if path.endswith(".html") and "templates" in parts[:-1]:
        name = "/".join(parts[len(parts) - parts[::-1].index("templates") :])
        entries = tuple(entry for entry, drawn in drawings().items() if name in drawn.templates)
        said = f"drawn by {_count(entries)}" if entries else "drawn by no catalog entry"
        return Reach(entries, f"{path}: {said}")
    raise AssertionError(f"catalog:{path}: a stylesheet under static/css/ or a template is what a page draws")


def _sheet(path: str) -> Reach:
    base = os.environ.get(BASE) or "HEAD"
    tip = _text(ROOT / path)
    was = _shown(base, path)
    hunks = _hunks(base, path)
    whole = not hunks and was is not None
    if whole:
        hunks = [((), range(1, tip.count("\n") + 2))]
    names: set[str] = set()
    every = False
    rules: list[str] = []
    for text, lines in [(was or "", [old for old, _ in hunks]), (tip, [new for _, new in hunks])]:
        for prelude, found in _reached(text, [line for each in lines for line in each]):
            rules.append(prelude)
            every = every or found is None
            names |= found or set()
    rules = list(dict.fromkeys(rules))
    drawn = drawings()
    entries = tuple(drawn) if every else tuple(entry for entry, held in drawn.items() if held.words & names)
    if not rules:
        return Reach((), f"{path}: its change is in no rule")
    changed = f"as {base} has it, so whole" if whole else "changed"
    said = f"{len(rules)} rule{'s' * (len(rules) != 1)} {changed} ({_listed(rules)})"
    reached = "every entry, as one names no class" if every else _count(entries) if entries else "no catalog entry"
    return Reach(entries, f"{path}: {said} reach{'es' * (len(rules) == 1)} {reached}")


def _hunks(base: str, path: str) -> list[tuple[Iterable[int], Iterable[int]]]:
    """Each changed run of lines of ``path`` since ``base``: (its lines there, its lines here)."""

    done = subprocess.run(
        ["git", "diff", "-U0", "--no-color", "--no-ext-diff", base, "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    found = []
    for old, old_count, new, new_count in _HUNK.findall(done.stdout):
        before, after = int(old), int(new)
        found.append(
            (
                range(before, before + int(old_count or 1)),
                range(after, after + int(new_count or 1)),
            )
        )
    return found


def _shown(base: str, path: str) -> str | None:
    done = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else None


def _text(file: Path) -> str:
    return file.read_text(encoding="utf-8") if file.is_file() else ""


def _reached(text: str, lines: list[int]) -> list[tuple[str, set[str] | None]]:
    """Each rule of ``text`` a line of ``lines`` lies in, comments aside, by its
    prelude, with the classes and data attributes it names (``_names``), or None
    where it reaches every entry."""

    if not lines:
        return []
    blocks = blocks_of(text)
    starts = [0, *(at + 1 for at, char in enumerate(text) if char == "\n")]
    bare = _COMMENT.sub(lambda comment: re.sub(r"[^\n]", " ", comment.group()), text)
    found: list[tuple[str, set[str] | None]] = []
    for line in sorted(set(lines)):
        if line > len(starts):
            continue
        start = starts[line - 1]
        end = starts[line] if line < len(starts) else len(text)
        if not bare[start:end].strip():
            continue
        around = [block for block in blocks if block.start < end and start < block.end]
        if not around:
            found.append((bare[start:end].strip(), None))
            continue
        inner = min(around, key=lambda block: block.end - block.start)
        index = blocks.index(inner)
        if inner.style:
            found.append((inner.prelude, _names(blocks, index)))
        elif inner.prelude.startswith(GROUPS):
            within = [at for at, block in enumerate(blocks) if block.style and _inside(blocks, at, index)]
            found += [(blocks[at].prelude, _names(blocks, at)) for at in within] or [(inner.prelude, None)]
        else:
            found.append((inner.prelude, None))
    return found


def _inside(blocks: list[Block], index: int, group: int) -> bool:
    parent = blocks[index].parent
    while parent is not None and parent != group:
        parent = blocks[parent].parent
    return parent == group


def _names(blocks: list[Block], index: int) -> set[str] | None:
    """What the style rule ``blocks[index]`` and the rules it is nested in name
    (``css_usage.named``), or None when an alternative of it names nothing and
    no rule around it does, so it styles any element."""

    chain: list[Block] = []
    at: int | None = index
    while at is not None:
        if blocks[at].style:
            chain.append(blocks[at])
        at = blocks[at].parent
    own, *around = ([named(_NOT.sub("", each)) for each in _alternatives(block.prelude)] for block in chain)
    outer = {word for alternatives in around for each in alternatives for word in each}
    if not outer and any(not each for each in own):
        return None
    return outer | {word for each in own for word in each}


def _alternatives(selector: str) -> list[str]:
    """A selector list's selectors, split on the commas outside parentheses."""

    found, depth, start = [], 0, 0
    for at, char in enumerate(selector):
        depth += (char == "(") - (char == ")")
        if char == "," and not depth:
            found.append(selector[start:at])
            start = at + 1
    return [*found, selector[start:]]


def _count(entries: tuple[str, ...]) -> str:
    return f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"


def _listed(rules: list[str]) -> str:
    shown = [Truncator(rule).chars(60) for rule in rules[:3]]
    rest = f", and {len(rules) - len(shown)} more" if len(rules) > len(shown) else ""
    return "; ".join(shown) + rest
