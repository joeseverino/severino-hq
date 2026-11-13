"""The words in a template that Vale cannot read, handed to Vale, and the
labels a template writes, held to the label rule.

Vale (``mise run lint:prose``) reads a template's text but not its attributes or
tags. ``shown`` finds the rest of what a person sees as words, and ``main``
hands each to Vale as a paragraph of its own, reported at the template line it
came from. Like ``interface_text``, a gate reads this and a running HQ never does.
lint:prose hands ``main`` Vale's report on the whole tree too, and ``held``
holds both to the count ``prose_baseline`` keeps of each file's findings.

A label (a heading, a tab, a fold's title, a column, a row group, a dialog's
title, a button, a link to another page) is the plain name a mature product's
dashboard gives the thing: a noun phrase of at most three words, "Access",
"History", "Source", "Installed on". It never opens with a question word and
holds no pronoun, no "everything", "each" or "every", and no relative clause:
those describe what the part holds, which is the lede's job. ``labels`` finds
every label a template writes and ``label_fault`` says what is wrong with one;
``main`` fails on both kinds of finding.
"""

import ast
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import override

from django.utils.text import smart_split

from .interface_text import TEMPLATE_COMMENT
from .prose_baseline import BASELINE, BASELINED_RULES

BASELINE_FILE = "hq/platform/core/prose_baseline.py"

_TEMPLATE_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
_TAG_LITERAL = re.compile(r"""(["'])((?:(?!\1).)*)\1""", re.DOTALL)
# What a browser shows or reads out from an element, besides its text.
_SHOWN_ATTRIBUTES = frozenset(
    {
        "title",
        "aria-label",
        "placeholder",
        "alt",
        "data-fragment-failure",
        "data-fragment-busy",
        "data-submit-label",
        "data-empty",
        "data-tip",
    }
)
# An identifier, a route, a path or a class list: lower case, no sentence in it.
_IDENTIFIER = re.compile(r"[a-z0-9_.:/#?&=%\- ]*\Z")
# The wording rules' config, in the checkout a gate runs from.
VALE_CONFIG = Path(__file__).resolve().parents[3] / "tools/lint/vale/.vale.ini"
# The extension Vale reads a piece as, which the config's [formats] names.
_EXTENSION = ".shown"


def _is_prose(text: str) -> bool:
    """Words a person reads: not an identifier, a path or a list of classes."""

    stripped = text.strip()
    if not stripped or stripped.endswith((".html", ".py", ".js", ".css")):
        return False
    return not _IDENTIFIER.match(stripped)


class _Attributes(HTMLParser):
    """The shown attributes of a template, with their lines."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, str]] = []

    @override
    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in _SHOWN_ATTRIBUTES and value:
                self.found.append((self.getpos()[0], value))


def _blank(match: re.Match) -> str:
    """The same lines, with nothing on them: positions after it stay true."""

    return "\n" * match.group().count("\n")


def shown(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` for each attribute and tag literal of a template a person reads."""

    uncommented = TEMPLATE_COMMENT.sub(_blank, source)
    found: list[tuple[int, str]] = []
    for tag in _TEMPLATE_TAG.finditer(uncommented):
        line = uncommented.count("\n", 0, tag.start()) + 1
        found.extend(
            (line, literal.group(2)) for literal in _TAG_LITERAL.finditer(tag.group()) if _is_prose(literal.group(2))
        )
    parser = _Attributes()
    parser.feed(_TEMPLATE_TAG.sub(_blank, uncommented))
    parser.close()
    found.extend(parser.found)
    return sorted(found)


# The longest a label runs: GitHub's, Stripe's and Cloudflare's run one to three.
LABEL_WORDS = 3
# A label that opens with one of these asks a question its part answers.
_QUESTION_WORDS = frozenset({"what", "who", "whom", "whose", "where", "how", "when", "why", "which", "whether", "if"})
# Words that make a label a sentence about its part: a pronoun, a relative
# clause, or a sweep over everything it holds. "Your" stays, as dashboards
# name the reader's own things ("Your repositories").
_SENTENCE_WORDS = frozenset(
    {"it", "its", "it's", "itself", "this", "these", "those", "they", "them", "their", "you", "that"}
    | {"everything", "each", "every"}
)
# The periods "this" names, as in "This week".
_PERIODS = frozenset({"day", "week", "month", "quarter", "year"})
# A name that breaks the rule, with why it stands as written.
PROPER_NAMES: dict[str, str] = {
    "Needs you": "the queue's one name, which Design's table of one name per thing sets",
}
# A template's value, one word of the label it stands in.
_VALUE = "\x00"
# A template tag inside a label's element: what is on either side is a label of
# its own, since a tag there is a branch or a part drawn apart.
_APART = "\x01"
_WORD = re.compile(r"[\w\x00][\w\x00'’.-]*")
# The tags whose first argument is a label, and the options of any tag that are.
_LABEL_TAGS = frozenset({"section", "table_heading", "row_group", "table_region", "post_button"})
_LABEL_OPTIONS = frozenset({"label", "more_label", "remedy_label", "action_label"})
# The block tag that draws a link around its words.
_LINK_TAG = "outside_link"
# An include of a dialog names its title.
_DIALOG = "partials/_modal.html"
# The elements whose words are a label.
_LABEL_ELEMENTS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "th", "button", "summary", "legend", "caption", "a"})
# Elements that hold running text with the rest of their parent, so a link in
# one is in the parent's sentence.
_INLINE = frozenset(
    {"span", "strong", "em", "b", "i", "small", "code", "kbd", "abbr", "time", "mark", "q", "sup", "sub"}
)
_VOID = frozenset(
    {"br", "hr", "img", "input", "meta", "link", "wbr", "source", "col", "area", "base", "embed", "track"}
)


def label_fault(text: str) -> str:
    """What makes ``text`` a sentence or a riddle rather than a label, or
    nothing when it is a label."""

    text = " ".join(text.split())
    if text in PROPER_NAMES:
        return ""
    words = [word.lower().removesuffix(".") for word in _WORD.findall(text)]
    shown = text.replace(_VALUE, "…")
    if not words:
        return ""
    if words[0] in _QUESTION_WORDS:
        return f"{shown!r} opens with {words[0]!r}: name the thing, and ask the question in the lede"
    # "This week" names a period, as a dashboard's range picker does.
    periods = {at for at, word in enumerate(words[:-1]) if word == "this" and words[at + 1] in _PERIODS}
    if found := sorted({word for at, word in enumerate(words) if word in _SENTENCE_WORDS and at not in periods}):
        return f"{shown!r} says {', '.join(map(repr, found))}: a label names the thing, and the lede says what it holds"
    if len(words) > LABEL_WORDS:
        return f"{shown!r} is {len(words)} words: a label is at most {LABEL_WORDS}, and the rest goes in the lede"
    return ""


def _literal(bit: str) -> str | None:
    """The text of a quoted template literal with no filter on it, else None."""

    if len(bit) > 1 and bit[0] == bit[-1] and bit[0] in "\"'":
        return bit[1:-1]
    return None


def _tag_labels(tag: str) -> Iterator[str]:
    """The labels one ``{% %}`` tag is handed as literals."""

    bits = list(smart_split(tag[2:-2].strip()))
    if not bits:
        return
    name, rest = bits[0], bits[1:]
    if name in _LABEL_TAGS and rest and "=" not in rest[0].split('"')[0] and (text := _literal(rest[0])) is not None:
        yield text
    dialog = name == "include" and rest and _literal(rest[0]) == _DIALOG
    for bit in rest:
        option, equals, value = bit.partition("=")
        if not equals or (text := _literal(value)) is None:
            continue
        if option in _LABEL_OPTIONS or (dialog and option == "title"):
            yield text


class _Labels(HTMLParser):
    """The words of each label element of a template, with their lines. A link
    with running text beside it is part of that sentence, and no label."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, str]] = []
        # Each open element: its name, its line, its words, whether running
        # text stands in it, and the links in it waiting on that answer.
        self._open: list[dict] = []

    @override
    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        self._open.append({"tag": tag, "line": self.getpos()[0], "words": [], "text": False, "links": []})

    @override
    def handle_endtag(self, tag):
        names = [frame["tag"] for frame in self._open]
        if tag not in names:
            return
        while self._open:
            frame = self._open.pop()
            self._closed(frame)
            if frame["tag"] == tag:
                return

    @override
    def handle_data(self, data):
        # A label's words are its own and its inline parts'; a paragraph or a
        # list inside it (a row's heading cell holds a row's notes) is no label.
        for frame in reversed(self._open):
            if frame["tag"] in _LABEL_ELEMENTS:
                frame["words"].append(data)
            elif frame["tag"] not in _INLINE:
                break
        # Text inside a link belongs to the link, not to the text around it.
        words = any(character.isalnum() or character == _VALUE for character in data)
        if words and self._open and self._open[-1]["tag"] != "a":
            self._open[-1]["text"] = True

    def _closed(self, frame: dict) -> None:
        parent = self._open[-1] if self._open else None
        inside_label = any(open_["tag"] in _LABEL_ELEMENTS for open_ in self._open)
        pieces = [(frame["line"], piece) for piece in "".join(frame["words"]).split(_APART)]
        if frame["tag"] == "a":
            if inside_label:
                return
            if parent is None:
                self.found.extend(pieces)
            else:
                parent["links"].extend(pieces)
            return
        if frame["tag"] in _LABEL_ELEMENTS:
            self.found.extend(pieces)
        links = frame["links"]
        if frame["tag"] in _INLINE and parent is not None:
            parent["links"].extend(links)
            parent["text"] = parent["text"] or frame["text"]
        elif not frame["text"]:
            self.found.extend(links)

    @override
    def close(self):
        super().close()
        while self._open:
            self._closed(self._open.pop())


def _apart(match: re.Match) -> str:
    """A tag as the label reader sees it: a value is a word, a tag that draws
    a link is the link, any other tag parts the words around it, and its
    lines stay."""

    tag, lines = match.group(), "\n" * match.group().count("\n")
    if tag.startswith("{{"):
        return f"{_VALUE}{lines}"
    name = tag[2:-2].split(maxsplit=1)[0] if tag[2:-2].strip() else ""
    return {_LINK_TAG: "<a>", f"end{_LINK_TAG}": "</a>"}.get(name, _APART) + lines


def labels(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` for each label a template writes, as a tag's literal or
    as the words of a label element."""

    uncommented = TEMPLATE_COMMENT.sub(_blank, source)
    found: list[tuple[int, str]] = []
    for tag in _TEMPLATE_TAG.finditer(uncommented):
        line = uncommented.count("\n", 0, tag.start()) + 1
        found.extend((line, text) for text in _tag_labels(tag.group()))
    parser = _Labels()
    parser.feed(_TEMPLATE_TAG.sub(_apart, uncommented))
    parser.close()
    found.extend((line, " ".join(text.split())) for line, text in parser.found)
    return sorted({(line, text) for line, text in found if text.strip(f" {_VALUE}")})


# The keywords and class attributes Python declares a label in: a page's
# title, a section's heading or label, a tab, a nav entry, a page action, a
# button's verb, a link's words.
_PYTHON_LABELS = frozenset({"label", "heading", "page_title", "verb", "more_label", "remedy_label", "action_label"})
# Callees whose ``label`` is no label. A step a person runs is a sentence. A
# command's name is its verb and its object, as ``labels.adding`` writes "Add
# an asset", and its button says its ``verb`` where one is set. A form field's
# label is a form's, and a policy tag's is its field's.
_NOT_LABELS = frozenset({"OperatorStep", "CapabilitySpec", "PolicyTag"})


# The parts that take their label as a positional argument, and where.
_POSITIONAL_LABELS = {"TableColumn": 0, "PageAction": 0, "PageSection": 1, "PageDialog": 1, "ServiceSection": 1}


def _callee(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    return function.attr if isinstance(function, ast.Attribute) else ""


def _strings(node: ast.AST | None) -> Iterator[tuple[int, str]]:
    """The literal strings ``node`` is, or a tuple or list of them holds."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.lineno, node.value
    elif isinstance(node, ast.Tuple | ast.List):
        for element in node.elts:
            yield from _strings(element)


def _call_labels(call: ast.Call) -> Iterator[tuple[int, str]]:
    callee = _callee(call)
    place = _POSITIONAL_LABELS.get(callee)
    if place is not None and len(call.args) > place:
        yield from _strings(call.args[place])
    for keyword in call.keywords:
        if keyword.arg == "label" and (callee in _NOT_LABELS or callee.endswith("Field")):
            continue
        if keyword.arg in _PYTHON_LABELS or (keyword.arg == "title" and callee == "PageDialog"):
            if isinstance(keyword.value, ast.Constant):
                yield from _strings(keyword.value)
        elif keyword.arg == "columns":
            yield from _strings(keyword.value)


def _declared(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield from _call_labels(node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            named = any(isinstance(target, ast.Name) and target.id in {"page_title", "heading"} for target in targets)
            if named and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                yield node.value.lineno, node.value.value


def python_labels(paths: Iterable[Path]) -> list[tuple[str, str]]:
    """``(path:line, text)`` for each label the Python under ``paths`` declares
    as a literal, its tests and migrations left out."""

    return [
        (f"{path}:{line}", text)
        for root in paths
        for path in sorted(root.rglob("*.py"))
        if not {"tests", "migrations"} & set(path.parts)
        for line, text in _declared(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    ]


def label_findings(pieces: Iterable[tuple[str, str]]) -> list[str]:
    """``where: Label: why`` for each label of ``(where, text)`` that breaks the
    label rule."""

    return [f"{where}: Label: {fault}" for where, text in pieces if (fault := label_fault(text))]


def _templates(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.html"))
        elif path.exists():
            yield path
        else:
            # A path that matches nothing is a moved file, and a rule that
            # reads nothing passes.
            raise FileNotFoundError(f"{path} is listed to be read and is not there.")


def document(paths: Iterable[Path]) -> tuple[str, list[tuple[Path, int]]]:
    """Every piece under ``paths`` as one document, a paragraph per line, and
    where each line of it came from."""

    pieces = [
        ((path, line), text)
        for path in _templates(paths)
        for line, text in shown(path.read_text(encoding="utf-8", errors="replace"))
    ]
    return _joined(text for _origin, text in pieces), [origin for origin, _text in pieces]


def _joined(texts: Iterable[str]) -> str:
    return "\n\n".join(" ".join(text.split()) for text in texts) + "\n"


def findings(alerts: dict, origins: list) -> list[str]:
    """Vale's alerts on the document, each at the place its piece came from:
    a template's ``(path, line)``, or any name a caller gives a piece."""

    found = []
    for alert in next(iter(alerts.values()), []):
        origin = origins[(alert["Line"] - 1) // 2]
        where = f"{origin[0]}:{origin[1]}" if isinstance(origin, tuple) else origin
        found.append(f"{where}: {alert['Check']}: {alert['Message']}")
    return found


def _vale(text: str) -> tuple[int, dict]:
    """Vale's exit status on ``text`` and its alerts, which an error leaves empty."""

    vale = subprocess.run(
        ["vale", f"--config={VALE_CONFIG}", f"--ext={_EXTENSION}", "--output=JSON"],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )
    if vale.returncode not in (0, 1):
        sys.stderr.write(vale.stdout + vale.stderr)
        return vale.returncode, {}
    return 0, json.loads(vale.stdout or "{}")


def refused(pieces: Iterable[tuple[str, str]]) -> list[str]:
    """Vale's findings on shown text that lives nowhere a file names it, such as
    a label HQ derives, each ``(where, text)`` reported at its ``where``."""

    pieces = list(pieces)
    status, alerts = _vale(_joined(text for _where, text in pieces))
    if status:
        raise subprocess.CalledProcessError(status, "vale")
    return findings(alerts, [where for where, _text in pieces])


def template_labels(paths: Iterable[Path]) -> list[tuple[str, str]]:
    """``(path:line, text)`` for each label the templates under ``paths`` write."""

    return [
        (f"{path}:{line}", text)
        for path in _templates(paths)
        for line, text in labels(path.read_text(encoding="utf-8", errors="replace"))
    ]


def tree_findings(alerts: dict) -> list[str]:
    """Vale's JSON report on the tree, one ``path:line:column: Check: Message`` each."""

    return [
        f"{path}:{alert['Line']}:{alert['Span'][0]}: {alert['Check']}: {alert['Message']}"
        for path, found in sorted(alerts.items())
        for alert in found
    ]


def _counted(finding: str) -> tuple[str, str]:
    """The ``(path, check)`` a finding is counted under in the baseline."""

    where, check = finding.split(": ", 2)[:2]
    return where.split(":", 1)[0], check


def held(found: list[str], *, whole: bool) -> list[str]:
    """``found`` less what ``prose_baseline.BASELINE`` allows each file, with a
    line for each file over its entry and, when ``whole`` (the whole tree was
    read), each under it, whose entry must come down."""

    counts = Counter(_counted(finding) for finding in found)
    kept = [finding for finding in found if counts[_counted(finding)] > BASELINE.get(_counted(finding), 0)]
    for (path, check), allowed in sorted(BASELINE.items()):
        have = counts[path, check]
        if have > allowed:
            kept.append(f"{path}: {check}: {have} where the baseline allows {allowed}; fix the new one")
        elif have < allowed and whole:
            kept.append(
                f"{path}: {check}: {have} where the baseline allows {allowed}; lower its entry in {BASELINE_FILE}"
            )
    return kept


def census(found: list[str]) -> str:
    """The baseline's entries for ``found``, as ``prose_baseline.BASELINE`` writes them."""

    counts = Counter(key for finding in found if (key := _counted(finding))[1] in BASELINED_RULES)
    return "\n".join(f"    ({path!r}, {check!r}): {count}," for (path, check), count in sorted(counts.items()))


def main(arguments: list[str]) -> int:
    """With ``--tree <report>``, Vale's JSON report on the whole tree joins
    what is read here and the baseline is held whole; ``--census`` prints the
    baseline's entries instead of failing."""

    tree, show_census = None, "--census" in arguments
    arguments = [argument for argument in arguments if argument != "--census"]
    if arguments[:1] == ["--tree"]:
        tree, arguments = Path(arguments[1]), arguments[2:]
    paths = [Path(argument) for argument in arguments]
    text, origins = document(paths)
    status, alerts = _vale(text)
    if status:
        return status
    read = template_labels(paths)
    found = findings(alerts, origins)
    if tree is not None:
        found = tree_findings(json.loads(tree.read_text(encoding="utf-8") or "{}")) + found
    if show_census:
        print(census(found))
        return 0
    found = held(found, whole=tree is not None) + label_findings(read)
    print(
        "\n".join(found)
        if found
        else f"{len(origins)} attributes and tag literals and {len(read)} labels read: no finding"
    )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
