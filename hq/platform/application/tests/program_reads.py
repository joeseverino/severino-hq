"""What a program a declared tree test starts reads of the checkout, from the paths it is handed.

reads.py's watch sees Python's own opens; a program it starts reads past them. Each program here is read
the way its own tool tells it: ast-grep by the files its ``--inspect entity`` names, Vale by the config
files and style paths its ``ls-config`` names, and ``git ls-files`` by its pathspecs. A scan by inline
rules that choose files by a language whose suffixes are known (``SUFFIXES``) is read without running
ast-grep twice: every file git does not ignore under each directory it is handed, with those suffixes,
which holds every file ast-grep reads there. A program, or a use of one, not read here is one the watch
cannot follow, and it fails a declared test.
"""

import functools
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Handed:
    """The files a program opens, and the directories it walks: any file an add under one puts there, it
    reads. Absolute paths."""

    opened: tuple[Path, ...] = ()
    walked: tuple[Path, ...] = ()


# Each option of ``ast-grep scan`` and ``run`` that takes the next word as its value.
AST_GREP_VALUES = frozenset({
    "-r", "--rule", "--rewrite", "--inline-rules", "--format", "--report-style", "--filter", "--min-severity",
    "-c", "--config", "--custom-languages", "--no-ignore", "--globs", "-j", "--threads", "--color",
    "--inspect", "-A", "--after", "-B", "--before", "-C", "--context", "--max-results", "-p", "--pattern",
    "--selector", "--strictness", "-k", "--kind", "-l", "--lang", "--heading",
})  # fmt: skip
VALE_VALUES = frozenset({"--config", "--output", "--minAlertLevel", "--filter", "--ext", "--glob"})
# ``git ls-files`` options that show a file's content (its blob, its line endings) beside its name.
LS_FILES_CONTENT = frozenset({"-s", "--stage", "-u", "--unmerged", "--debug", "--eol", "--format"})
LS_FILES_VALUES = frozenset({"-x", "--exclude", "-X", "--exclude-from", "--exclude-per-directory", "--abbrev"})
# What ``git rev-parse`` answers from git's own records alone, never the work tree's files.
REV_PARSE_OWN = frozenset({
    "--git-path", "--show-toplevel", "--git-dir", "--git-common-dir", "--absolute-git-dir",
    "--is-inside-work-tree", "--show-prefix", "--show-cdup", "--path-format",
})  # fmt: skip


ROOT = Path(__file__).resolve().parents[4]
# The suffixes ast-grep reads a language's files by, as its --inspect entity shows it assigning them.
SUFFIXES = {"python": frozenset({".py", ".py3", ".pyi", ".bzl"})}


def handed(program: str, words: list[str], cwd: Path) -> Handed | None:
    """What ``program`` run with ``words`` in ``cwd`` reads, or None where that cannot be told."""

    reader = READERS.get(program)
    return reader(words, cwd) if reader else None


def _split(words: list[str], values: frozenset[str]) -> tuple[dict[str, list[str]], list[str]]:
    """The options among ``words`` with their values, and the positional words; ``--`` ends the options."""

    options: dict[str, list[str]] = {}
    positional: list[str] = []
    rest = iter(words)
    for word in rest:
        if word == "--":
            positional.extend(rest)
            break
        if word.startswith("-") and word != "-":
            name, equals, value = word.partition("=")
            if not equals and name in values:
                value = next(rest, "")
            options.setdefault(name, []).append(value)
        else:
            positional.append(word)
    return options, positional


def _ast_grep(words: list[str], cwd: Path) -> Handed | None:
    if words[:1] not in (["scan"], ["run"]):
        return None
    options, paths = _split(words[1:], AST_GREP_VALUES)
    if "--stdin" in options:
        return Handed()
    # A scan of the project's own rules reads its config's rule directories, which are not told here.
    if words[0] == "scan" and not ({"--inline-rules", "-r", "--rule"} & options.keys()):
        return None
    if words[0] == "run" and not ({"-p", "--pattern"} & options.keys()):
        return None
    handed_paths = [cwd / path for path in paths or ["."]]
    rules = [cwd / rule for rule in [*options.get("-r", []), *options.get("--rule", [])] if words[0] == "scan"]
    extensions = _extensions(words, options)
    if extensions is None:
        scanned = [cwd / path for path in _ast_grep_files(("ast-grep", *words), str(cwd))]
    else:
        scanned = [path for path in handed_paths if not path.is_dir()]
        scanned += [
            file for path in handed_paths if path.is_dir() for file in _under(path) if file.suffix in extensions
        ]
    return Handed(opened=(*rules, *scanned), walked=tuple(path for path in handed_paths if path.is_dir()))


def _extensions(words: list[str], options: dict[str, list[str]]) -> frozenset[str] | None:
    """The suffixes of the files a scan by inline rules reads, when they choose files by language alone and
    every language is one ast-grep's suffixes are known for; None where only ast-grep can tell."""

    if not options.get("--inline-rules") or {"--globs", "--no-ignore", "-c", "--config"} & options.keys():
        return None
    try:
        rules = [json.loads(text) for text in options["--inline-rules"][-1].split("\n---\n")]
    except ValueError:
        return None
    if not all(isinstance(rule, dict) and not {"files", "ignores"} & rule.keys() for rule in rules):
        return None
    languages = {str(rule.get("language", "")).lower() for rule in rules}
    if not languages <= SUFFIXES.keys() or (ROOT / "sgconfig.yml").exists():
        return None
    return frozenset().union(*(SUFFIXES[language] for language in languages))


def _under(directory: Path) -> list[Path]:
    """The files of the checkout under ``directory`` that git does not ignore, which is what ast-grep walks
    there; none for a directory outside it, whose reads the watch never checks."""

    try:
        prefix = directory.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return []
    start = "" if prefix == "." else prefix + "/"
    return [ROOT / path for path in _checkout_files() if path.startswith(start)]


@functools.cache
def _checkout_files() -> tuple[str, ...]:
    done = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(path for path in done.stdout.split("\0") if path)


@functools.cache
def _ast_grep_files(argv: tuple[str, ...], cwd: str) -> tuple[str, ...]:
    """The files ast-grep reads for ``argv``, as its ``--inspect entity`` names them, and its project config
    when it finds one: the same run again, its matches thrown away."""

    done = subprocess.run(
        [*argv, "--inspect", "entity"], cwd=cwd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
    )
    found: list[str] = []
    for line in done.stderr.splitlines():
        kind, _, rest = line.removeprefix("sg: ").partition(": ")
        if kind.startswith("entity|file|"):
            found.append(kind.removeprefix("entity|file|"))
        elif kind == "summary|project" and "isProject=true" in rest:
            directory = rest.partition("projectDir=")[2].partition(",")[0]
            found.append(str(Path(directory) / "sgconfig.yml"))
    return tuple(found)


def _vale(words: list[str], cwd: Path) -> Handed | None:
    options, paths = _split(words, VALE_VALUES)
    # Without a config named, Vale looks for one up from each file it reads.
    if not options.get("--config") or (paths and paths[0].startswith("ls-")) or "--glob" in options:
        return None
    config = json.loads(_vale_config(options["--config"][-1], str(cwd)))
    styles = [Path(path) for path in config.get("Paths") or ()]
    named = [cwd / path for path in [*(config.get("ConfigFiles") or ()), *paths]]
    filters = [cwd / value for value in options.get("--filter", []) if (cwd / value).is_file()]
    walked = [path for path in [*styles, *named] if path.is_dir()]
    return Handed(
        opened=(*(path for path in named if not path.is_dir()), *filters, *_files(walked)),
        walked=tuple(walked),
    )


@functools.cache
def _vale_config(config: str, cwd: str) -> str:
    return subprocess.run(
        ["vale", f"--config={config}", "ls-config"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _files(directories: list[Path]) -> list[Path]:
    """Every file under ``directories``, which a program that reads a directory whole reads."""

    return [Path(top) / name for directory in directories for top, _, names in os.walk(directory) for name in names]


def _git(words: list[str], cwd: Path) -> Handed | None:
    if words[:1] == ["rev-parse"]:
        options, revisions = _split(words[1:], frozenset({"--git-path"}))
        # A revision's path (``HEAD:docs/x.md``) is a file's content.
        own = words[1:] and options.keys() <= REV_PARSE_OWN and not any(":" in each for each in revisions)
        return Handed() if own else None
    if words[:1] == ["init"]:
        # It writes a repository, reading only git's own templates.
        return Handed()
    if words[:1] != ["ls-files"]:
        return None
    options, pathspecs = _split(words[1:], LS_FILES_VALUES)
    roots = [_pathspec_root(spec) for spec in pathspecs or ["."]]
    if "--with-tree" in options or "" in roots:
        return None
    excludes = [cwd / name for flag in ("-X", "--exclude-from") for name in options.get(flag, [])]
    # A file's mode or blob beside its name is its content, so each file listed is read.
    shown = [cwd / path for path in _listed(tuple(words), str(cwd))] if LS_FILES_CONTENT & options.keys() else []
    if any(spec.startswith(":(") and "attr:" in spec.partition(")")[0] for spec in pathspecs):
        shown += [ROOT / path for path in _checkout_files() if PurePosixPath(path).name == ".gitattributes"]
    return Handed(opened=(*excludes, *shown), walked=tuple(cwd / root for root in roots if root is not None))


@functools.cache
def _listed(words: tuple[str, ...], cwd: str) -> tuple[str, ...]:
    """The paths ``git ls-files`` lists for ``words``, its content options left out."""

    kept = [word for word in words[1:] if word.partition("=")[0] not in LS_FILES_CONTENT and word != "-z"]
    done = subprocess.run(["git", "ls-files", "-z", *kept], cwd=cwd, capture_output=True, text=True, check=True)
    return tuple(path for path in done.stdout.split("\0") if path)


def _pathspec_root(spec: str) -> str | None:
    """The directory a pathspec lists below, its path up to its first wildcard: None for an exclusion, which
    lists nothing, and "" for magic not told here (``:/``, ``:(top)``)."""

    magic = ""
    if spec.startswith(":("):
        magic, _, spec = spec[2:].partition(")")
    elif spec.startswith(":") and spec[1:2] in ("!", "^"):
        return None
    elif spec.startswith(":"):
        return ""
    words = set(magic.split(",")) - {""}
    if words & {"exclude"}:
        return None
    # An attribute's files are those its .gitattributes name, read beside the listing (_git).
    if {word for word in words if not word.startswith("attr:")} - {"literal", "glob"}:
        return ""
    parts = []
    for part in spec.split("/"):
        if "literal" not in words and any(char in part for char in "*?["):
            break
        parts.append(part)
    return "/".join(parts) or "."


READERS: dict[str, Callable[[list[str], Path], Handed | None]] = {
    "ast-grep": _ast_grep,
    "sg": _ast_grep,
    "vale": _vale,
    "git": _git,
}
