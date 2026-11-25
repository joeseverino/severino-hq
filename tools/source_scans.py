"""`mise run lint:source-scans [files]`: no tracked text file holds a command form that breaks something.

Each scan names the files it skips, with why, and the one way to do it right.
They read shell, Python, TOML, YAML and Dockerfiles alike, where a command line
can sit in any of them, so they are text scans rather than ast-grep rules,
which parse one language a file.

Git file lists: git quotes a name holding a backslash, a double quote or a
newline in a list of lines, core.quotepath or not, so a reader that splits on
lines drops or mangles it (systemd's escaping writes such names under
deploy/systemd). In Python a list goes through
``hq.platform.core.tests.task.names``, which asks for ``-z`` itself; a command
line elsewhere carries ``-z``.

Mise state: mise records each config it trusts or loads in its state
directory, and its reshim walks every record there, so a run that trusts a
throwaway project through ``MISE_TRUSTED_CONFIG_PATHS`` and leaves
``MISE_STATE_DIR`` alone adds a record that outlives the project. The trust is
set in one place per language, which sets the state beside it:
``trusted_mise`` in ``hq.platform.core.tests.task`` and ``mise_trust`` in
``tests/shell/mise-state.sh``.
"""

import ast
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
# This module and its cases, which spell each refused form.
HERE = frozenset({"tools/source_scans.py", "tools/tests/test_source_scans.py"})

# A git command line, to the end of its pipeline stage: a backslash-newline
# continues it, as a shell script wraps one.
COMMAND = re.compile(r"\bgit\b(?:\\\n|[^\n|;&)`])*")
WORD = re.compile(r"[^\s\"',\[\]()\\]+")
# The variable given a value: a shell or Dockerfile assignment, a Python
# keyword argument, a dict key or an item set on os.environ.
SETS = re.compile(r"""\bMISE_TRUSTED_CONFIG_PATHS\b["']?\]?\s*[:=]""")


def lists_names(words: list[str]) -> bool:
    """Whether a git command's words ask for a list of file names one per line."""

    return ("ls-files" in words or "--name-only" in words) and "-z" not in words


def shell_findings(text: str, offset: int = 0) -> list[tuple[int, str]]:
    """(line, command) for each git command line in ``text`` that lists names by line."""

    found = []
    for match in COMMAND.finditer(text):
        if lists_names(WORD.findall(match.group())):
            line = offset + text.count("\n", 0, match.start()) + 1
            found.append((line, " ".join(match.group().split())))
    return found


def docstrings(tree: ast.AST) -> set[int]:
    """The ids of the string constants that are docstrings, which are prose."""

    found = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        first = body[0] if isinstance(body, list) and body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            found.add(id(first.value))
    return found


def strings(nodes: list[ast.expr]) -> list[str]:
    return [node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def python_findings(text: str) -> list[tuple[int, str]]:
    """(line, code) for each git call in ``text`` that lists names by line: an argument
    list that starts with "git", a call of a function named ``git``, or a command line
    in a string."""

    tree = ast.parse(text)
    prose = docstrings(tree)
    found = []
    for node in ast.walk(tree):
        words = None
        if isinstance(node, (ast.List, ast.Tuple)) and strings(node.elts)[:1] == ["git"]:
            words = strings(node.elts)
        elif isinstance(node, ast.Call):
            if (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "git":
                words = ["git", *strings(node.args)]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose:
            found.extend(shell_findings(node.value, node.lineno - 1))
        if words is not None and lists_names(words):
            found.append((node.lineno, ast.get_source_segment(text, node) or ""))
    return found


def git_list_findings(path: str, text: str) -> list[tuple[int, str]]:
    if path.endswith(".py"):
        try:
            return python_findings(text)
        except SyntaxError:
            return shell_findings(text)
    return shell_findings(text)


def mise_trust_findings(path: str, text: str) -> list[tuple[int, str]]:
    """(line, text) for each line of ``text`` that sets MISE_TRUSTED_CONFIG_PATHS, prose apart."""

    if path.endswith(".md"):
        return []
    return [(number, line.strip()) for number, line in enumerate(text.splitlines(), 1) if SETS.search(line)]


class Scan(NamedTuple):
    findings: Callable[[str, str], list[tuple[int, str]]]
    # Files the scan skips, each with why.
    exempt: frozenset[str]
    fix: str


SCANS = {
    "git file lists": Scan(
        git_list_findings,
        frozenset(
            {
                *HERE,
                # Tests only that the generated files exist, and prints a stale
                # one to a person: no name is read.
                "controller/scripts/check.sh",
            }
        ),
        "these read git's file names one per line, which quotes a name holding a backslash: "
        "pass -z and split on NUL (hq.platform.core.tests.task.names in Python, "
        "tr '\\0' '\\n' or perl -0 in sh)",
    ),
    "mise state": Scan(
        mise_trust_findings,
        frozenset(
            {
                *HERE,
                # The two helpers, which set the state beside it.
                "hq/platform/core/tests/task.py",
                "tests/shell/mise-state.sh",
                # Each runs in a container whose state goes with it: the images,
                # and the Playwright container, whose HOME is a volume of its own.
                "Dockerfile",
                "composition/Dockerfile",
                "tools/dev/browser-linux.sh",
                # Runs on the second machine, whose own state holds the heavy
                # turn every gate run there shares.
                "tools/gate/remote-host.sh",
            }
        ),
        "these trust a mise config and leave its records in the Mac's mise state: use "
        "hq.platform.core.tests.task.trusted_mise in Python or mise_trust from "
        "tests/shell/mise-state.sh in sh, which keep MISE_STATE_DIR in the run's own directory",
    ),
}


def tracked_texts(named: list[str] | None = None) -> list[tuple[str, str]]:
    """Each of ``named`` (by default every file git tracks) that reads as text, by path."""

    if not named:
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True)
        named = listed.stdout.split("\0")
    texts = []
    for name in filter(None, named):
        path = ROOT / name
        # A file that reads as no text holds no command line.
        with suppress(UnicodeDecodeError):
            if path.is_file() and not path.is_symlink():
                texts.append((name, path.read_text()))
    return texts


def main(named: list[str]) -> int:
    """Print each finding of every scan in ``named``, or every tracked file, under the scan's fix."""

    texts = tracked_texts(named)
    failed = False
    for scan in SCANS.values():
        found = [
            f"{path}:{line}: {code}"
            for path, text in texts
            if path not in scan.exempt
            for line, code in scan.findings(path, text)
        ]
        if found:
            failed = True
            print(f"{scan.fix}:", *found, sep="\n", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
