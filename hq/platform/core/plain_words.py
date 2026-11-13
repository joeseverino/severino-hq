"""The words in a template that Vale cannot read, handed to Vale.

Vale (``mise run lint:prose``) reads a template's text but not its attributes or
tags. ``shown`` finds the rest of what the reader sees as words, and ``main``
hands each to Vale as a paragraph of its own, reported at the template line it
came from. Like ``interface_text``, a gate reads this and a running HQ never does.
lint:prose runs ``main --whole``, which has Vale read the whole tree too.
"""

import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import override

from .interface_text import TEMPLATE_COMMENT

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
# What lint:prose hands Vale, from the repository's root, and what it leaves out.
TREE = (
    *("hq", "hq_sdk", "templates", "static", "controller", "tools", "tests", "demo", "deploy", "composition"),
    *("docs", "mise-tasks", "AGENTS.md", "README.md", "CLAUDE.md", "manage.py", "entrypoint.sh"),
)
_TREE_GLOB = "--glob=!{**/node_modules/**,**/vendor/**,**/__pycache__/**,**/*.gen.go}"
# What plain_words reads itself: the templates' pieces.
WHOLE = ("templates", "hq", "hq_sdk")
# Vale's listing of the text Plain reads, which no gate fails on.
_SHOWN = ("--minAlertLevel=suggestion", '--filter=.Name == "Plain.Shown"')
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
                # Each branch of an attribute is shown alone, never run on into the next.
                self.found.extend((self.getpos()[0], branch) for branch in value.split(_BRANCH) if branch.strip())


def _blank(match: re.Match) -> str:
    """The same lines, with nothing on them: positions after it stay true."""

    return "\n" * match.group().count("\n")


# What an ``{% else %}`` or an ``{% elif %}`` stands as in an attribute.
_BRANCH = "\x02"


def _branched(match: re.Match) -> str:
    """A tag blanked, but a branch's separator kept, so the words on either
    side are read as the alternatives they are."""

    name = (match.group()[2:-2].split() or [""])[0] if match.group().startswith("{%") else ""
    return (_BRANCH if name in {"else", "elif"} else "") + _blank(match)


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
    parser.feed(_TEMPLATE_TAG.sub(_branched, uncommented))
    parser.close()
    found.extend(parser.found)
    return sorted(found)


def _templates(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.html"))
        elif path.exists():
            if path.suffix != ".py":
                yield path
        else:
            # A path that matches nothing is a moved file, and a rule that
            # reads nothing passes.
            raise FileNotFoundError(f"{path} is listed to be read and is not there.")


def _pieces(paths: Iterable[Path]) -> list[tuple[tuple[Path, int], str]]:
    return [
        ((path, line), text)
        for path in _templates(paths)
        for line, text in shown(path.read_text(encoding="utf-8", errors="replace"))
    ]


def document(paths: Iterable[Path]) -> tuple[str, list[tuple[Path, int]]]:
    """Every piece under ``paths`` as one document, a paragraph per line, and
    where each line of it came from."""

    pieces = _pieces(paths)
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


def _vale(text: str = "", files: Iterable[Path | str] = (), flags: Iterable[str] = ()) -> tuple[int, dict]:
    """Vale's exit status and its alerts, which an error leaves empty: on
    ``files`` as lint:prose reads the tree, or else on ``text``."""

    files = [str(path) for path in files]
    vale = subprocess.run(
        ["vale", f"--config={VALE_CONFIG}", "--output=JSON", *flags, *(files or [f"--ext={_EXTENSION}"])],
        input=None if files else text,
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


def tree_findings(alerts: dict) -> list[str]:
    """Vale's JSON report on the tree, one ``path:line:column: Check: Message`` each."""

    return [
        f"{path}:{alert['Line']}:{alert['Span'][0]}: {alert['Check']}: {alert['Message']}"
        for path, found in sorted(alerts.items())
        for alert in found
    ]


def dump(alerts: dict, pieces: Iterable[tuple[tuple[Path, int], str]]) -> list[str]:
    """``path:line: text`` for each line of text the rules read: Vale's, as
    its ``Plain.Shown`` alerts give them, and the pieces read here."""

    lines: dict[tuple[str, int], list[str]] = {}
    for path, found in alerts.items():
        for alert in sorted(found, key=lambda alert: alert["Span"][0]):
            lines.setdefault((path, alert["Line"]), []).append(" ".join(alert["Match"].split()))
    for (path, line), text in pieces:
        lines.setdefault((str(path), line), []).append(" ".join(text.split()))
    return [f"{path}:{line}: {' '.join(texts)}" for (path, line), texts in sorted(lines.items())]


def by_file(lines: list[str]) -> str:
    """How many of ``dump``'s lines each file holds, most first, then the total."""

    counts = Counter(line.split(":", 1)[0] for line in lines)
    rows = [f"{count:6} {path}" for path, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    return "\n".join([*rows, f"{len(lines):6} lines in {len(counts)} files"])


def main(arguments: list[str]) -> int:
    """With ``--whole``, run from the repository's root, Vale reads ``TREE``
    and plain_words ``WHOLE``; otherwise both read the ``paths`` given.
    ``--dump`` prints each line of text the rules read, at its file and line,
    and ``--by-file`` how many lines each file holds."""

    flags = {"--whole", "--dump", "--by-file"} & set(arguments)
    whole = "--whole" in flags
    paths = [Path(path) for path in WHOLE] if whole else [Path(a) for a in arguments if a not in flags]
    tree, tree_flags = (TREE, (_TREE_GLOB,)) if whole else (paths, ())
    if {"--dump", "--by-file"} & flags:
        return _dumped(paths, tree, tree_flags, counted="--by-file" in flags)
    status, found, read = _found(paths, tree, tree_flags)
    if status:
        return status
    print("\n".join(found) if found else f"{read} read: no finding")
    return 1 if found else 0


def _dumped(paths: list[Path], tree: Iterable[Path | str], tree_flags: tuple[str, ...], *, counted: bool) -> int:
    status, alerts = _vale(files=tree, flags=(*tree_flags, *_SHOWN))
    if status:
        return status
    lines = dump(alerts, _pieces(paths))
    print(by_file(lines) if counted else "\n".join(lines))
    return 0


def _found(paths: list[Path], tree: Iterable[Path | str], tree_flags: tuple[str, ...]) -> tuple[int, list[str], str]:
    """Vale's exit status, every finding on ``paths`` and on ``tree``, and what
    was read."""

    text, origins = document(paths)
    status, alerts = _vale(text)
    if status:
        return status, [], ""
    found = findings(alerts, origins)
    status, alerts = _vale(files=tree, flags=tree_flags)
    if status:
        return status, [], ""
    return 0, tree_findings(alerts) + found, f"{len(origins)} attributes and tag literals"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
