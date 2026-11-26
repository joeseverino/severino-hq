"""audit:fact-values: every ``{% fact %}`` whose value a template writes as a
sentence, and every value drawn from a domain object's wording.

A fact's value is its short answer, its reason the detail (the ``fact``
part's rules, docs/Shared-UI.md). A value is the text before the fact's first
part (``{% detail %}``) or its ``value=``, with tags taken out and each
variable read as a word. Each branch (``{% if %}``, ``{% for %}``) and each
item of a list is read apart. It is a sentence when it holds a stop or a
colon followed by words, ends in a stop, or runs past four words. A value
taken from a wording (``*_said``, ``.words``, ``.note``) is listed for a
reader to judge, since only its object knows its words."""

import re
import sys
from collections.abc import Iterator
from pathlib import Path

from django.template.base import Lexer, TokenType

BRANCHES = {"if", "elif", "else", "endif", "for", "empty", "endfor", "dot", "with", "endwith"}
PARTS = {"detail", "heading", "panel", "foot", "endfact"}
APART = re.compile(r"</?li\b[^>]*>")
WORDED = re.compile(r"\b[\w.]*(?:_said|\.said|\.words|\.reason|\.why|\.note)\b")


def sentence(text: str) -> bool:
    text = re.sub(r"\s+", " ", text).strip()
    if re.search(r"[.:;]\s+\w|\.$", text):
        return True
    return len(re.findall(r"[A-Za-z']+", text)) > 4


def values(source: str) -> Iterator[tuple[int, str, list[str], list[str]]]:
    """Each fact's line, label, its value's branches as words and the variables it draws."""

    tokens = Lexer(source).tokenize()
    at = 0
    while at < len(tokens):
        token = tokens[at]
        at += 1
        if token.token_type != TokenType.BLOCK or token.contents.split()[:1] != ["fact"]:
            continue
        bits = token.split_contents()
        label = bits[1] if len(bits) > 1 else ""
        given = dict(bit.split("=", 1) for bit in bits[2:] if "=" in bit)
        if "value" in given:
            value = given["value"]
            yield token.lineno, label, [value[1:-1]] if value[:1] in "\"'" else [], [value]
            continue
        words, variables, quiet = [], [], False
        while at < len(tokens) and not _ends_value(tokens[at]):
            each = tokens[at]
            head = each.contents.split()[:1] if each.token_type == TokenType.BLOCK else []
            quiet = {"comment": True, "endcomment": False}.get(head[0] if head else "", quiet)
            if head and head[0] in BRANCHES:
                words.append("\0")
            elif quiet:
                pass
            elif each.token_type == TokenType.TEXT:
                words.append(APART.sub("\0", each.contents))
            elif each.token_type == TokenType.VAR:
                words.append(" X ")
                variables.append(each.contents)
            at += 1
        text = re.sub(r"<[^>]*>", " ", "".join(words))
        yield token.lineno, label, [branch for branch in text.split("\0") if branch.strip()], variables


def _ends_value(token) -> bool:
    return token.token_type == TokenType.BLOCK and (token.contents.split() or [""])[0] in PARTS


def main(roots: list[Path]) -> None:
    sentences = worded = 0
    for root in roots:
        for path in sorted(root.rglob("*.html")):
            for line, label, branches, variables in values(path.read_text()):
                where = f"{path}:{line} {label}"
                for branch in filter(sentence, branches):
                    sentences += 1
                    print(f"sentence  {where}: {re.sub(r'\s+', ' ', branch).strip()}")
                for name in (name for drawn in variables for name in WORDED.findall(drawn)):
                    worded += 1
                    print(f"wording   {where}: {name}")
    print(f"\n{sentences} hand-written sentence values, {worded} values from a wording")


if __name__ == "__main__":
    import django
    from django.conf import settings

    settings.configure()
    django.setup()
    main([Path(root) for root in sys.argv[1:]] or [Path("templates")])
