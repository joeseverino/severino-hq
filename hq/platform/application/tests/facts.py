"""What ast-grep finds in this checkout's Python, for the source checks that judge facts across files.

ast-grep parses in a process of its own, so a check reads back only the nodes
its rules match, and no test process holds a parsed tree. A rule is ast-grep's
rule object (https://ast-grep.github.io/reference/rule.html) as a dict, and a
check runs all of its rules in one scan. The image ships no ast-grep, so only
a test tagged ``source`` scans, which test_unshipped_tag.py holds.
"""

import ast
import json
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class Found:
    """One node a rule matched: the rule's id, where it is, and its source text."""

    rule: str
    path: Path
    line: int
    start: int
    end: int
    text: str
    # Each single metavariable the rule captured, as its text.
    captured: dict[str, str]

    def value(self, name: str | None = None) -> object:
        """The literal the match (or a captured metavariable) spells, or None when it is no literal."""

        try:
            return ast.literal_eval(self.captured[name] if name else self.text)
        except ValueError, SyntaxError:
            return None


# A statement of the module itself, in no block. Said as what it is not inside:
# a match reports each node a relational rule matched, so `inside: module`
# would send the whole module back with every match, and scan refuses it.
TOP: dict[str, Any] = {"not": {"inside": {"kind": "block", "stopBy": "end"}}}


# A string literal whole: an implicit concatenation as the one string it makes,
# and a string inside an f-string's field as part of that f-string. One may
# span lines, so it is parsed in parentheses.
_LITERAL: dict[str, Any] = {"any": [{"kind": "concatenated_string"}, {"kind": "string"}]}
STRING: dict[str, Any] = {**_LITERAL, "not": {"inside": {**_LITERAL, "stopBy": "end"}}}


def strings(found: Found) -> list[str]:
    """Each piece of text a STRING match spells, an f-string's fixed parts each on its own."""

    literal = ast.parse(f"({found.text})", mode="eval")
    return [node.value for node in ast.walk(literal) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def scan(rules: dict[str, dict[str, Any]], *paths: str | Path) -> list[Found]:
    """Every match of ``rules``, by id, in the Python under ``paths`` (the checkout by default).

    ast-grep reads a directory as git does, leaving out what .gitignore names
    and hidden directories.
    """

    # ast-grep names a path that is not there and still exits 0, so a check
    # whose path moved would read nothing and pass.
    if missing := [path for path in paths if not (ROOT / path).exists()]:
        raise FileNotFoundError(f"ast-grep would scan nothing at {', '.join(map(str, missing))}")
    if whole := sorted(rule for rule, body in rules.items() if _inside_module(body)):
        raise ValueError(
            f"{', '.join(whole)}: a rule inside the module would send the whole module back with every match;"
            " say a statement of the module itself with facts.TOP"
        )
    inline = "\n---\n".join(
        json.dumps({"id": rule, "language": "python", "severity": "hint", "rule": body}) for rule, body in rules.items()
    )
    # Read as it streams, one match at a time: the whole answer at once would
    # weigh more than the facts kept from it.
    # Its complaints go to a file, which no unread pipe can fill and stall.
    with (
        tempfile.TemporaryFile("w+") as complaints,
        subprocess.Popen(
            ["ast-grep", "scan", "--json=stream", "--inline-rules", inline, *(map(str, paths) if paths else ".")],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=complaints,
            text=True,
        ) as running,
    ):
        assert running.stdout is not None
        files: dict[str, Path] = {}
        found = [_found(json.loads(line), files) for line in running.stdout]
        running.wait()
        if running.returncode:
            complaints.seek(0)
            raise RuntimeError(f"ast-grep could not scan:\n{complaints.read()}")
    return found


def _inside_module(rule: object) -> bool:
    """Whether an ``inside`` anywhere in ``rule`` can match the module, itself or through a branch of it."""

    def names_module(target: object) -> bool:
        if not isinstance(target, dict):
            return False
        branches = [*target.get("any", []), *target.get("all", [])]
        return target.get("kind") == "module" or any(names_module(branch) for branch in branches)

    if isinstance(rule, list):
        return any(_inside_module(item) for item in rule)
    if not isinstance(rule, dict):
        return False
    return names_module(rule.get("inside")) or any(_inside_module(value) for value in rule.values())


def _found(match: dict[str, Any], files: dict[str, Path]) -> Found:
    file = match["file"]
    if file not in files:
        files[file] = (ROOT / file).resolve()
    return Found(
        rule=match["ruleId"],
        path=files[file],
        line=match["range"]["start"]["line"] + 1,
        start=match["range"]["byteOffset"]["start"],
        end=match["range"]["byteOffset"]["end"],
        text=match["text"],
        captured={name: held["text"] for name, held in match.get("metaVariables", {}).get("single", {}).items()},
    )


# ----- Imports ----------------------------------------------------------------

IMPORT: dict[str, Any] = {
    "any": [{"kind": "import_statement"}, {"kind": "import_from_statement"}, {"kind": "future_import_statement"}]
}
# Where an import waits rather than running with its module: each a rule over
# IMPORT. A lazy import's keyword parses as an ERROR leaf just before it.
WAITS: dict[str, dict[str, Any]] = {
    "lazy": {"follows": {"kind": "ERROR", "regex": "^lazy$"}},
    "called": {"inside": {"kind": "function_definition", "stopBy": "end"}},
    "typed": {
        "inside": {
            "stopBy": "end",
            "kind": "block",
            "inside": {
                "field": "consequence",
                "kind": "if_statement",
                "has": {"field": "condition", "regex": r"^(typing\.)?TYPE_CHECKING$"},
            },
        }
    },
    # In a try that carries on when what it imports is not installed.
    "guarded": {
        "inside": {
            "stopBy": "end",
            "kind": "block",
            "inside": {
                "field": "body",
                "kind": "try_statement",
                "has": {
                    "any": [
                        {"kind": "except_clause", "not": {"has": {"field": "value", "pattern": "$CAUGHT"}}},
                        {
                            "kind": "except_clause",
                            "has": {"field": "value", "regex": r"\b(ImportError|ModuleNotFoundError)\b"},
                        },
                    ],
                },
            },
        }
    },
}


@dataclass(frozen=True)
class Import:
    """One import statement: what it names, and each way it waits (``WAITS``)."""

    path: Path
    line: int
    # The module of a ``from`` import, with its leading dots; None for a plain import.
    source: str | None
    # Each name it binds from there, or each module a plain import names, with its ``as``.
    names: tuple[tuple[str, str | None], ...]
    waits: frozenset[str]
    # Whether it is a statement of the module itself, outside any block.
    top: bool

    def modules(self, package: str) -> list[str]:
        """Each full module name the import names, read from ``package`` (the importing module's package)."""

        if self.source is None:
            return [name for name, _ in self.names]
        level = len(self.source) - len(self.source.lstrip("."))
        head = self.source[level:]
        if level:
            parts = package.split(".")
            anchor = parts[: len(parts) - level + 1]
            head = ".".join([*anchor, head] if head else anchor)
        return [head, *(f"{head}.{name}" for name, _ in self.names)]

    def bound(self) -> list[str]:
        """The names the import binds in its scope."""

        if self.source is None:
            return [alias or name.partition(".")[0] for name, alias in self.names]
        return [alias or name for name, alias in self.names]


def imports(*paths: str | Path) -> list[Import]:
    """Every import statement in the Python under ``paths``, at any depth."""

    found = scan(
        {"import": IMPORT, "top": {**IMPORT, **TOP}} | {wait: {**IMPORT, **rule} for wait, rule in WAITS.items()},
        *paths,
    )
    waits: dict[tuple[Path, int], set[str]] = {}
    for match in found:
        if match.rule != "import":
            waits.setdefault((match.path, match.start), set()).add(match.rule)
    return [_import(match, waits.get((match.path, match.start), set())) for match in found if match.rule == "import"]


def _import(match: Found, marks: set[str]) -> Import:
    statement = ast.parse(match.text).body[0]
    assert isinstance(statement, ast.Import | ast.ImportFrom)
    names = tuple((alias.name, alias.asname) for alias in statement.names)
    source = None
    if isinstance(statement, ast.ImportFrom):
        source = "." * statement.level + (statement.module or "")
    return Import(match.path, match.line, source, names, frozenset(marks - {"top"}), "top" in marks)


def module_name(path: Path, root: Path = ROOT) -> str:
    """The dotted name the module at ``path`` is imported by from ``root``."""

    parts = path.relative_to(root.resolve()).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def package_of(path: Path, root: Path = ROOT) -> str:
    """The package a module's relative imports are read from."""

    name = module_name(path, root)
    return name if path.name == "__init__.py" else name.rpartition(".")[0]


def grouped(found: Iterable[Found]) -> dict[str, list[Found]]:
    """Matches by the rule that found them."""

    by_rule: dict[str, list[Found]] = {}
    for match in found:
        by_rule.setdefault(match.rule, []).append(match)
    return by_rule
