"""The words in a template that Vale cannot read, handed to Vale.

Vale (``mise run lint:prose``) reads a template's text but not its attributes or
tags. ``shown`` finds the rest of what a person sees as words, and ``main``
hands each to Vale as a paragraph of its own, reported at the template line it
came from. Like ``interface_text``, a gate reads this and a running HQ never does.
"""

import json
import re
import subprocess
import sys
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


def main(arguments: list[str]) -> int:
    text, origins = document(Path(argument) for argument in arguments)
    status, alerts = _vale(text)
    if status:
        return status
    found = findings(alerts, origins)
    print("\n".join(found) if found else f"{len(origins)} attributes and tag literals read: no finding")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
