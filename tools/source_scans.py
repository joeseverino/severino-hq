"""`mise run lint:source-scans [files]`: no tracked text file holds a form that breaks something.

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

Reachable endpoints: this repository is public, so an address, a host key or a
deployment's own name committed here is published whether or not anything
treats it as a secret; what stays here is the shape.
"""

import ast
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from functools import cache
from pathlib import Path, PurePosixPath
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


# Documentation, private, multicast and reserved ranges are examples or
# classes, not places. 192.168.0.0/16 is the exception: a host from it is
# somebody's actual machine, so only the range's base passes, as half of a
# CIDR. A fixture wanting a private address uses 10.0.0.0/8.
RESERVED = re.compile(
    r"^(?:127\.|10\.|192\.168\.0\.0$|169\.254\.|0\.|255\.|22[4-9]\.|2[3-5][0-9]\.|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])\.|"
    r"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|"
    r"203\.0\.113\.|198\.51\.100\.|192\.0\.2\.|192\.0\.0\.|"
    r"1\.1\.1\.1|8\.8\.8\.8)"
)
ADDRESS = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
# Long enough to be key material: a real ed25519 public key is 68 base64
# characters, and a fixture's short stand-in passes.
HOST_KEY = re.compile(r"ssh-(?:ed25519|rsa) AAAA[A-Za-z0-9+/]{32,}")
# A hostname under a deployment's own private zone, which names one
# installation's topology. `example` and `invalid` are for writing about hostnames.
PRIVATE_HOST = re.compile(
    r"\b[a-z0-9-]+\.(?!example\b|invalid\b|test\b|localhost\b)"
    r"(?:homelab|lan|internal|local)\b(?!\.(?:example\.)?toml\b)"
)


@cache
def deployment_terms() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The addresses the deployment publishes on purpose, and the terms no file may name.

    Names a checkout's own deployment goes by, one per line, kept in
    .git/info where nothing is tracked or pushed. A line starting with `!` is
    an address the deployment publishes on purpose (a public demo), which a
    file may name although a term covers it.
    """

    where = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/deployment-terms"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    lines: list[str] = []
    with suppress(OSError):
        lines = [
            line.strip().lower()
            for line in (ROOT / where.stdout.strip()).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    published = tuple(line[1:] for line in lines if line.startswith("!"))
    return published, tuple(line for line in lines if not line.startswith("!"))


def endpoint_findings(_path: str, text: str) -> list[tuple[int, str]]:
    """(line, what) for each reachable endpoint ``text`` names: an address, a host key, a private host or a term."""

    def at(start: int) -> int:
        return text.count("\n", 0, start) + 1

    # A dotted number over 255 is a version, and one after `section-` a specification's section.
    found = [
        (at(match.start()), match.group())
        for match in ADDRESS.finditer(text)
        if all(int(part) <= 255 for part in match.group().split("."))
        and text[max(0, match.start() - 8) : match.start()] != "section-"
        and not RESERVED.match(match.group())
    ]
    found += [(at(match.start()), "ssh host key") for match in HOST_KEY.finditer(text)]
    found += [(at(match.start()), match.group()) for match in PRIVATE_HOST.finditer(text)]
    published, terms = deployment_terms()
    lowered = text.lower()
    for address in published:
        lowered = lowered.replace(address, " " * len(address))
    found += [
        (at(lowered.index(term)), f"deployment term #{index + 1}")
        for index, term in enumerate(terms)
        if term in lowered
    ]
    return found


class Scan(NamedTuple):
    findings: Callable[[str, str], list[tuple[int, str]]]
    # Globs of the files the scan skips, each with why.
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
    "reachable endpoints": Scan(
        endpoint_findings,
        frozenset(
            {
                *HERE,
                # Lockfiles and pinned action SHAs are hashes, not hosts; vendored
                # bundles carry upstream examples, pinned by their UPSTREAM.
                "**/package-lock.json",
                "**/uv.lock",
                ".github/**",
                "controller/api/vendor/**",
                "static/vendor/**",
            }
        ),
        "these name a reachable endpoint, a deployment fact this public repository would publish: keep it in "
        "1Password, which reaches the controller through the connection registry's env. A dotted number that is "
        "no address passes as section-3.1.3.7, and a fixture's host key written under 32 base64 characters after AAAA",
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
            if not any(map(PurePosixPath(path).full_match, scan.exempt))
            for line, code in scan.findings(path, text)
        ]
        if found:
            failed = True
            print(f"{scan.fix}:", *found, sep="\n", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
