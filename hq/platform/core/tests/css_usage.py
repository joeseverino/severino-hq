"""How much of HQ's stylesheet the pages use, for `mise run audit:css-usage`.

Chromium records which style rules match while it styles a page: its
rule-usage tracking (CDP's ``CSS.startRuleUsageTracking`` and
``CSS.takeCoverageDelta``), what Chrome DevTools' Coverage panel reads.
Playwright's Python API has no ``page.coverage``, so the run asks a CDP
session for it directly. A static reader (PurgeCSS) cannot see a class the
server chooses as it renders or a script adds as it runs; the browser sees
every class a page reaches.

The run draws the pages `mise run dev:look` draws, on its seeded server, and
on each page opens every disclosure, fold and row, reads it at each width in
both themes, forces the interaction states (hover, focus, active, target) on
every element, and opens each dialog the page declares. A rule no page
matched is then sorted by why: one inside a media query no page matched
(print, forced colors, a coarse pointer, a width the run does not read) or a
feature test Chromium fails is apart; one whose classes and data attributes
the source names is unreached, a state no seed raised; the rest ask for
something nothing writes.

No Python CSS parser says where a rule ends (tinycss2 keeps only where a node
starts), so ``blocks_of`` reads each rule's span itself: comments, strings
and braces are the whole of CSS's block structure.
"""

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import override

from django.conf import settings
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.stalls import stall_watch
from hq.platform.core.stylesheets import DIRECTORY, SHEETS, STATIC
from hq.platform.core.tests import look_shares
from hq.platform.core.tests.browser.widths import WIDTHS, viewport
from hq.platform.core.tests.look import _DIALOGS, Look, this_run

# What a forced state makes every element at once, as DevTools' :hov pane
# forces it on one.
FORCED = ["hover", "focus", "focus-visible", "focus-within", "active", "target"]
# The at-rules whose condition the page answers: each held or not wherever it is read.
CONDITIONS = ("@media", "@supports")
# A pseudo-class or pseudo-element of one engine's own.
_VENDOR = re.compile(r"::?-(?:moz|webkit|ms)-")
# Which of the conditions it is handed hold on the page as it stands.
_HOLDING = """(preludes) => preludes.filter((prelude) => prelude.startsWith('@media')
  ? matchMedia(prelude.slice(6).trim()).matches
  : CSS.supports(prelude.startsWith('@supports') ? prelude.slice(9).trim() : prelude))"""
# Where the source that writes a page's classes lives, tests left out.
SOURCE = (("templates", "*.html"), ("static/js", "*.js"), ("hq", "*.py"), ("hq_sdk", "*.py"))
_SHEET_URL = re.compile(rf"/{re.escape(DIRECTORY)}/([\w-]+?)(?:\.[0-9a-f]{{12}})?\.css(?:[?#]|$)")
_FLUSH = "(theme) => { if (theme) document.documentElement.dataset.theme = theme; return document.body.offsetHeight; }"
_THEME_AT = "() => document.documentElement.dataset.theme ?? ''"
_THEME_BACK = "(theme) => { if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme; }"
# A share's record of what its pages used, beside the look's own.
SHARE_RECORD = "css-usage-{}.json"


@dataclass(frozen=True)
class Block:
    """One block of a sheet: a style rule or an at-rule with a body."""

    start: int  # where its prelude begins, as an index into the text
    end: int  # just past its closing brace
    opens: int  # its opening brace
    line: int
    prelude: str  # the selector or at-rule, its whitespace collapsed
    parent: int | None  # the block it is nested in, by index
    own: int  # its bytes outside the blocks nested in it
    keyframe: bool = False  # a step of an @keyframes, which Chromium's rule usage never tracks

    @property
    def style(self) -> bool:
        return not self.prelude.startswith("@") and not self.keyframe


_SKIPPED = re.compile(r"(?:\s+|/\*.*?(?:\*/|\Z))*", re.DOTALL)
_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)


def _string_end(text: str, at: int) -> int:
    quote, i = text[at], at + 1
    while i < len(text) and text[i] not in (quote, "\n"):
        i += 2 if text[i] == "\\" else 1
    return i + 1


def blocks_of(text: str) -> list[Block]:
    """Every block in ``text``, each listed once its body closes."""

    spans: list[tuple[int, int, int | None, str, int]] = []  # start, end, parent, prelude, opens
    stack: list[tuple[int, str, int]] = []  # start, prelude, slot reserved in spans
    statement = 0
    i = 0
    while i < len(text):
        char = text[i]
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = len(text) if close < 0 else close + 2
            continue
        if char in "\"'":
            i = _string_end(text, i)
            continue
        if char == "\\":
            i += 2
            continue
        if char == "{":
            start = _SKIPPED.match(text, statement).end()
            prelude = " ".join(_COMMENT.sub(" ", text[start:i]).split())
            spans.append((start, -1, stack[-1][2] if stack else None, prelude, i))
            stack.append((start, prelude, len(spans) - 1))
            statement = i + 1
        elif char == "}":
            if stack:
                start, prelude, slot = stack.pop()
                spans[slot] = (start, i + 1, spans[slot][2], prelude, spans[slot][4])
            statement = i + 1
        elif char == ";":
            statement = i + 1
        i += 1
    nested: dict[int, int] = defaultdict(int)
    sizes = [len(text[start:end].encode()) for start, end, _parent, _prelude, _opens in spans]
    for index, (_start, _end, parent, _prelude, _opens) in enumerate(spans):
        if parent is not None:
            nested[parent] += sizes[index]
    return [
        Block(
            start,
            end,
            opens,
            text.count("\n", 0, start) + 1,
            prelude,
            parent,
            sizes[index] - nested[index],
            parent is not None and spans[parent][3].startswith("@keyframes"),
        )
        for index, (start, end, parent, prelude, opens) in enumerate(spans)
        if end >= 0
    ]


def code_points(text: str) -> Callable[[int], int]:
    """An offset Chromium gives, in UTF-16 units, as an index into ``text``."""

    if all(ord(char) < 0x10000 for char in text):
        return lambda offset: offset
    at: list[int] = []
    for index, char in enumerate(text):
        at.extend([index] * (2 if ord(char) >= 0x10000 else 1))
    at.append(len(text))
    return lambda offset: at[min(offset, len(at) - 1)]


def used_blocks(blocks: list[Block], ranges: Iterable[tuple[int, int]]) -> tuple[set[int], list[tuple[int, int]]]:
    """The style rules ``ranges`` say were used, by index, and the ranges that
    matched none. A range is a rule's whole span, header to closing brace: the
    widest rule inside it, or for declarations after a nested rule (which
    Chromium tracks as a rule of their own), the innermost rule around them.
    A range on a media or container query's condition says only that it
    applied, which the rules inside it say for themselves."""

    used: set[int] = set()
    unmatched: list[tuple[int, int]] = []
    style = [(index, block) for index, block in enumerate(blocks) if block.style]
    conditions = [block for block in blocks if not block.style]
    for start, end in ranges:
        if any(block.start <= start < block.opens for block in conditions):
            continue
        inside = [
            (block.end - block.start, index) for index, block in style if start <= block.start and block.end <= end + 1
        ]
        if inside:
            used.add(max(inside)[1])
            continue
        around = [(block.end - block.start, index) for index, block in style if block.start <= start < block.end]
        if around:
            used.add(min(around)[1])
        else:
            unmatched.append((start, end))
    return used, unmatched


def _chain(blocks: list[Block], index: int) -> list[Block]:
    """The blocks around ``blocks[index]``, outermost first."""

    chain, parent = [], blocks[index].parent
    while parent is not None:
        chain.insert(0, blocks[parent])
        parent = blocks[parent].parent
    return chain


def _condition(block: Block) -> str:
    """What ``block`` holds only where the browser answers yes: its media
    query or feature test, or, for a rule naming an engine's own pseudo
    (``::-moz-progress-bar``), whether the browser parses its selector, its
    nesting ``&`` read as any element; else ""."""

    if block.prelude.startswith(CONDITIONS):
        return block.prelude
    if block.style and _VENDOR.search(block.prelude):
        return f"selector({block.prelude.replace('&', '*')})"
    return ""


def conditions(blocks: list[Block]) -> set[str]:
    """Every condition ``blocks`` hold (``_condition``)."""

    return {held for block in blocks if (held := _condition(block))}


def apart(blocks: list[Block], index: int, held: set[str]) -> str:
    """Why an unused rule is listed apart from the dead, or "": a condition
    on it or around it (``_condition``) that held on no page read."""

    for block in [*_chain(blocks, index), blocks[index]]:
        if (condition := _condition(block)) and condition not in held:
            return condition
    return ""


# A name's prefix where the source builds the rest in: a template's or an
# f-string's interpolation (`tone-{{ tone }}`, `tone-${tone}`), or a join
# (`"tone-" + tone`). Kept with its hyphen, which no whole word ends with.
_BUILT = re.compile(r"([\w-]*\w-)(?:\{|\$\{|[\"']\s*[+%])")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_CLASS = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
_DATA = re.compile(r"\[\s*(data-[\w-]+)")


def named(selector: str) -> list[str]:
    """The classes and data attributes ``selector`` names."""

    bare = _QUOTED.sub('""', selector)
    return [*_CLASS.findall(bare), *_DATA.findall(bare)]


def source_words(root: Path) -> frozenset[str]:
    """Every word the source that writes a page's markup holds."""

    words: set[str] = set()
    for folder, pattern in SOURCE:
        for path in (root / folder).rglob(pattern):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            words.update(re.findall(r"[\w-]*\w", text))
            words.update(_BUILT.findall(text))
    return frozenset(words)


def written(name: str, words: frozenset[str]) -> bool:
    """Whether the source can write ``name``: whole, by a prefix it is built
    from (``tone-`` before a tone), or a data attribute as a dataset key or a
    Python keyword."""

    if name in words or any(word.endswith("-") and name.startswith(word) for word in words):
        return True
    if name.startswith("data-"):
        rest = name.removeprefix("data-")
        camel = re.sub(r"-([a-z])", lambda match: match.group(1).upper(), rest)
        return camel in words or name.replace("-", "_") in words
    return False


@dataclass
class Sheet:
    """What one sheet's rules came to."""

    name: str
    bytes: int
    blocks: list[Block]
    used: set[int]
    unmatched: list[str]  # each range no rule held, as its line and text

    def unused(self) -> list[int]:
        return [index for index, block in enumerate(self.blocks) if block.style and index not in self.used]


def sorted_unused(sheet: Sheet, words: frozenset[str], held: set[str]) -> dict[str, list[int]]:
    """Each unused rule of ``sheet`` by why: ``apart`` (``held`` names the
    conditions that held somewhere), ``unreached`` (the
    source names all it asks for) or ``dead`` (it asks for a class or data
    attribute nothing writes, or sits in a rule that does)."""

    sorted_: dict[str, list[int]] = {"dead": [], "unreached": [], "apart": []}
    verdict: dict[int, str] = {}
    for index in sheet.unused():
        parent = sheet.blocks[index].parent
        while parent is not None and not sheet.blocks[parent].style:
            parent = sheet.blocks[parent].parent
        if apart(sheet.blocks, index, held):
            verdict[index] = "apart"
        elif parent in verdict and verdict[parent] == "dead":
            verdict[index] = "dead"
        elif all(written(name, words) for name in named(sheet.blocks[index].prelude)):
            verdict[index] = "unreached"
        else:
            verdict[index] = "dead"
        sorted_[verdict[index]].append(index)
    return sorted_


def unused_bytes(sheet: Sheet, indices: Iterable[int]) -> int:
    """The bytes of the rules at ``indices``, with every at-rule all of whose
    rules are among them, as a cut would remove them."""

    gone = set(indices)
    total = sum(sheet.blocks[index].own for index in gone)
    for block in sheet.blocks:
        if block.style:
            continue
        inside = [
            each for each, other in enumerate(sheet.blocks) if other.style and block.start < other.start < block.end
        ]
        if inside and all(each in gone for each in inside):
            total += block.own
    return total


def sheets(used: dict[str, set[tuple[int, int]]], served: dict[str, str]) -> list[Sheet]:
    """Each app sheet, in cascade order, with the rules ``used`` names: read
    as the page was ``served`` it, where Chromium's offsets point, since
    static serving names a ``url()`` by its hashed file. Its lines are the
    file's, as that renaming adds none."""

    out = []
    for path in SHEETS:
        name = Path(path).stem
        text = served.get(name) or (STATIC / path).read_text(encoding="utf-8")
        blocks = blocks_of(text)
        units = code_points(text)
        indices, unmatched = used_blocks(blocks, ((units(start), units(end)) for start, end in used.get(name, ())))
        said = [
            f"line {text.count(chr(10), 0, start) + 1}: {' '.join(text[start:end].split())[:80]}"
            for start, end in unmatched
        ]
        size = (STATIC / path).stat().st_size
        out.append(Sheet(name, size, blocks, indices, said))
    return out


HEADS = {
    "dead": "Unused, asking for a class or data attribute nothing in the source writes",
    "unreached": "Unused, all it asks for written somewhere in the source, drawn by no page read (a state no seed raises)",
    "apart": "Apart, under a media query no page read matched, a feature test Chromium fails, or a selector it does not parse",
}


def report(found: list[Sheet], words: frozenset[str], held: set[str], read: int, unread: dict[str, str]) -> list[str]:
    """The lines the audit prints: a table of each sheet's bytes and the bytes
    of its unused rules, then each unused rule by why, as file:line."""

    lines = [
        (
            f"{read} pages read at each width in both themes, every disclosure open, interaction states forced,"
            " each dialog open; Chromium's rule usage"
        ),
        "",
        f"{'sheet':<16}{'bytes':>8}{'unused':>8}{'share':>7}{'dead':>8}{'unreached':>11}{'apart':>8}{'rules':>7}{'unused':>8}",
    ]
    listed: dict[str, list[str]] = {kind: [] for kind in HEADS}
    totals = [0] * 7
    for sheet in found:
        kinds = sorted_unused(sheet, words, held)
        dead, unreached, set_apart = (unused_bytes(sheet, kinds[kind]) for kind in ("dead", "unreached", "apart"))
        unused = unused_bytes(sheet, kinds["dead"] + kinds["unreached"])
        rules = sum(block.style for block in sheet.blocks)
        row = [sheet.bytes, unused, dead, unreached, set_apart, rules, len(kinds["dead"]) + len(kinds["unreached"])]
        totals = [total + value for total, value in zip(totals, row, strict=True)]
        lines.append(_row(f"{sheet.name}.css", row))
        for kind, indices in kinds.items():
            for index in indices:
                block = sheet.blocks[index]
                where = f"static/{DIRECTORY}/{sheet.name}.css:{block.line}"
                why = f"  [{apart(sheet.blocks, index, held)}]" if kind == "apart" else ""
                listed[kind].append(f"  {where}  {block.prelude}  ({block.own} B){why}")
    lines.append(_row("total", totals))
    for sheet in found:
        lines += [
            f"{sheet.name}.css: a used range matched no rule read from the file, {each}" for each in sheet.unmatched
        ]
    if unread:
        lines += ["", "Not read whole:", *(f"  {name}: {why}" for name, why in unread.items())]
    for kind, head in HEADS.items():
        lines += ["", f"{head} ({len(listed[kind])}):", *listed[kind]]
    return lines


def _row(name: str, row: list[int]) -> str:
    size, unused, dead, unreached, set_apart, rules, unused_rules = row
    share = f"{unused * 100 // size}%" if size else "-"
    return f"{name:<16}{size:>8}{unused:>8}{share:>7}{dead:>8}{unreached:>11}{set_apart:>8}{rules:>7}{unused_rules:>8}"


def sheet_named(url: str) -> str:
    """The app sheet ``url`` serves, by name, or ""."""

    match = _SHEET_URL.search(url)
    return match.group(1) if match else ""


class CssUsage(Look):
    """The look's pages, read for the style rules they match."""

    cdp = None

    @override
    def setUp(self):
        # Kept across the browser contexts the run starts: a catalog entry is
        # drawn without script, in a context of its own.
        self.ids: dict[str, str] = {}
        self.used: dict[str, set[tuple[int, int]]] = defaultdict(set)
        self.served: dict[str, str] = {}
        self.unread: dict[str, str] = {}
        self.holding: set[str] = set()
        self.preludes = sorted(set().union(*(conditions(sheet.blocks) for sheet in sheets({}, {}))))
        super().setUp()

    @override
    def start(self, **options):
        # What the page about to be closed used, while it still can say.
        self.harvest()
        super().start(**options)
        self.cdp = self.context.new_cdp_session(self.page)
        self.cdp.on("CSS.styleSheetAdded", self.sheet_added)
        for method in ("DOM.enable", "CSS.enable", "CSS.startRuleUsageTracking"):
            self.cdp.send(method)

    def sheet_added(self, event):
        header = event["header"]
        if name := sheet_named(header.get("sourceURL", "")):
            self.ids[header["styleSheetId"]] = name

    def harvest(self):
        """The rules used since the last harvest, kept by sheet."""

        if self.cdp is None:
            return
        for usage in self.cdp.send("CSS.takeCoverageDelta")["coverage"]:
            sheet = self.ids.get(usage["styleSheetId"])
            if sheet and usage["used"]:
                self.used[sheet].add((usage["startOffset"], usage["endOffset"]))
                if sheet not in self.served:
                    text = self.cdp.send("CSS.getStyleSheetText", {"styleSheetId": usage["styleSheetId"]})["text"]
                    self.served[sheet] = text

    def test_css_usage(self):
        self.asked_for()
        widths = self.widths_asked or list(WIDTHS)
        self.draw_with_script(True)
        read = 0
        for name in self.asked:
            stall_watch.mark(f"audit:css-usage {name}")
            print(f"Reading {name}", flush=True)
            if refused := self.refused(name, widths[0]):
                self.unread[name] = refused
                continue
            try:
                self.read_page(name, widths)
                read += 1
            except BrowserError as broke:
                self.unread[name] = f"read in part ({str(broke).splitlines()[0]})"
            self.harvest()
        self.harvest()
        mine = {"used": {sheet: sorted(ranges) for sheet, ranges in self.used.items()}, "served": self.served}
        mine |= {"unread": self.unread, "read": read, "held": sorted(self.holding)}
        if (at := look_shares.this_share()) and (shared := look_shares.folder()):
            (shared / SHARE_RECORD.format(at[0])).write_text(json.dumps(mine), encoding="utf-8")
            return
        for run in self.shares:
            run.process.wait()
            run.relay.join()
            path = this_run() / SHARE_RECORD.format(run.index)
            if not path.is_file():
                self.unread[f"share {run.index}"] = "ended without its record; its pages were not read"
                continue
            theirs = json.loads(path.read_text(encoding="utf-8"))
            for sheet, ranges in theirs["used"].items():
                self.used[sheet].update(map(tuple, ranges))
            self.served |= theirs["served"]
            self.unread |= theirs["unread"]
            self.holding.update(theirs["held"])
            read += theirs["read"]
        found = sheets(self.used, self.served)
        words = source_words(Path(settings.BASE_DIR))
        print("", *report(found, words, self.holding, read, self.unread), sep="\n", flush=True)

    def read_page(self, name, widths):
        """``name`` with each dialog it declares open, opened from the page as
        it loaded as a reader opens one; then with everything a reader can
        open opened, read at each width in both themes; and last with
        interaction states forced, which nothing after it clears. A dialog
        that would not open is said, and the page loaded again without it."""

        if self.runs_script:
            for dialog in self.page.evaluate(_DIALOGS):
                try:
                    if not self.open_dialog(name, widths[0], dialog):
                        continue
                    self.open_every_disclosure(name)
                    for width in widths:
                        self.read_at(width)
                    self.close_dialog(dialog)
                except BrowserError as broke:
                    self.unread[f"{name} #modal-{dialog}"] = f"not read ({str(broke).splitlines()[0]})"
                    self.open(name, widths[0])
        self.page.set_viewport_size(viewport(widths[0]))
        self.open_every_disclosure(name)
        self.open_everything(name, widths[0])
        self.expand_rows()
        for width in widths:
            self.read_at(width)
        self.read_forced(widths)

    def read_at(self, width):
        """The page styled at ``width`` in each theme, then as it was, and
        which media queries and feature tests held there."""

        self.page.set_viewport_size(viewport(width))
        self.settle()
        theme = self.page.evaluate(_THEME_AT)
        for scheme in ("light", "dark"):
            self.page.emulate_media(color_scheme=scheme)
            self.page.evaluate(_FLUSH, scheme)
        self.page.emulate_media(color_scheme="null")
        self.page.evaluate(_THEME_BACK, theme)
        self.page.evaluate(_FLUSH, "")
        self.holding.update(self.page.evaluate(_HOLDING, self.preludes))

    def read_forced(self, widths):
        """The page styled with every element hovered, focused, active and
        targeted at once, at the narrowest and the widest width."""

        root = self.cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        nodes = self.cdp.send("DOM.querySelectorAll", {"nodeId": root, "selector": "*"})["nodeIds"]
        self.send_together("CSS.forcePseudoState", [{"nodeId": node, "forcedPseudoClasses": FORCED} for node in nodes])
        for width in sorted({widths[0], widths[-1]}):
            self.page.set_viewport_size(viewport(width))
            self.page.evaluate(_FLUSH, "")

    def send_together(self, method, each):
        """``method`` sent once for each of ``each`` without waiting on the
        one before: one at a time, a page's thousands of elements take a
        round trip each. Playwright's sync session has no batch, so its async
        half is handed them together; a node a script removed meanwhile is
        refused, which changes nothing."""

        session = self.cdp._impl_obj

        async def sent():
            return await asyncio.gather(*(session.send(method, params) for params in each), return_exceptions=True)

        # On the sync session's own loop, as each of its calls runs.
        self.cdp._sync(sent())
