"""Static conformance checks for plugin source trees.

Two boundaries, both read without importing the plugin or configuring Django:
its Python imports only ``hq_sdk``, and its templates use only the public
pieces of the host's (``contract.json``'s ``templates`` section).
"""

import argparse
import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

lazy from django.template.base import Lexer, TokenType

# These are implementation packages in the public host repository. Plugins get
# their supported equivalents from hq_sdk; importing one of these makes a host
# refactor a coordinated multi-repository migration.
#
# A list written out by hand drifts: a new host app missing from it lets a
# plugin import that app while the boundary check passes. A check that silently
# stops checking is worse than no check, because the architecture test suite
# reports it as green.
#
# So the set is *derived* from the host tree and *floored* by this list. Union,
# never replacement: a new host app is caught the day it appears, and a host
# tree that cannot be read (an SDK installed without its host, a future
# packaging change) still enforces everything known at the time this shipped.
# The boundary can get stricter on its own. It cannot get weaker on its own.
_FLOOR = frozenset(
    {
        "analytics",
        "application",
        "assets",
        "calendars",
        "config",
        "contacts",
        "content",
        "control_plane",
        "core",
        "docs_index",
        "example_hq_plugin",
        "expenses",
        "hq",
        "hq_api",
        "hq_mcp",
        "jobs",
        "projects",
        "receipts",
        "reports",
        "search_index",
    }
)

# hq_sdk is the supported surface; it is the one host package a plugin may name.
_SUPPORTED_FACADE = "hq_sdk"


def _host_packages() -> frozenset[str]:
    """Top-level packages of the host this SDK was installed from."""

    root = Path(__file__).resolve().parents[1]
    try:
        entries = list(root.iterdir())
    except OSError:
        return frozenset()
    return frozenset(
        entry.name
        for entry in entries
        if entry.is_dir() and (entry / "__init__.py").exists() and entry.name != _SUPPORTED_FACADE
    )


HOST_INTERNAL_PACKAGES = _FLOOR | _host_packages()


def _imported_modules(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    """The absolute module names one import statement names."""

    if isinstance(node, ast.ImportFrom):
        # A relative import names the plugin's own package, never the host's,
        # however its module is spelled: `from .calendars import x` is the
        # plugin's own calendars.
        return (node.module,) if node.module and not node.level else ()
    return tuple(alias.name for alias in node.names)


def _file_violations(source_path: Path, root: Path) -> list[str]:
    """Each host-internal import in one file; an unreadable file is one itself."""

    relative = source_path.relative_to(root)
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [f"{relative}:1: {exc}"]
    return [
        f"{relative}:{node.lineno}: {module}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for module in _imported_modules(node)
        if module.partition(".")[0] in HOST_INTERNAL_PACKAGES
    ]


def unsupported_hq_imports(source_root: str | Path) -> list[str]:
    """Return stable ``path:line: module`` violations for host-internal imports."""

    root = Path(source_root).resolve()
    violations: list[str] = []
    for source_path in sorted(root.rglob("*.py")):
        violations.extend(_file_violations(source_path, root))
    return sorted(set(violations))


# Template checks -----------------------------------------------------------

_CONTRACT = Path(__file__).with_name("contract.json")
_REGISTERS = frozenset({"filter", "simple_tag", "inclusion_tag", "tag", "simple_block_tag"})
# Tags that continue or close another rather than standing alone.
_CONTINUATIONS = frozenset({"else", "elif", "empty", "plural"})
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_FILTER = re.compile(r"\|\s*(\w+)(\s*:)?")
_TEMPLATE_TAG = re.compile(r"\{[{%#].*?[}%#]\}", re.DOTALL)
_CLASS = re.compile(r"\bclass=\"([^\"]*)\"")
_HOOK = re.compile(r"\s(data-[a-z][a-z0-9-]*)")


def _is_register(node: ast.AST) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr in _REGISTERS


def _named(call: ast.Call) -> str | None:
    """The name a ``register.filter(...)`` call gives: ``name=``, a string, or the function's own."""

    given = [keyword.value for keyword in call.keywords if keyword.arg == "name"] + call.args[:1]
    for node in given:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
    return None


def _registered(node: ast.AST) -> tuple[str, ...]:
    """The tag or filter names one function definition or registering call adds."""

    if isinstance(node, ast.FunctionDef):
        return tuple(
            (_named(decorator) if isinstance(decorator, ast.Call) else None) or node.name
            for decorator in node.decorator_list
            if _is_register(decorator)
        )
    if isinstance(node, ast.Call) and _is_register(node):
        name = _named(node)
        return (name,) if name else ()
    return ()


def _own_libraries(root: Path) -> dict[str, frozenset[str]]:
    libraries = {}
    for module in sorted(root.rglob("templatetags/*.py")):
        if module.name == "__init__.py":
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        libraries[module.stem] = frozenset(name for node in ast.walk(tree) for name in _registered(node))
    return libraries


def _own_templates(root: Path) -> dict[str, Path]:
    """Every file under a ``templates`` directory: Django loads any of them by name."""

    found = {}
    for directory in sorted(root.rglob("templates")):
        if directory.is_dir():
            for template in sorted(directory.rglob("*")):
                if template.is_file():
                    found[template.relative_to(directory).as_posix()] = template
    return found


def _arguments(bits: list[str]) -> tuple[list[str], set[str]]:
    """A tag's positional arguments and keyword names, without a trailing ``as name``."""

    if len(bits) >= 2 and bits[-2] == "as":
        bits = bits[:-2]
    positional = [bit for bit in bits if not re.match(r"^\w+=", bit)]
    keywords = {bit.partition("=")[0] for bit in bits if re.match(r"^\w+=", bit)}
    return positional, keywords


def _signature_problem(parameters: list[str], bits: list[str]) -> str:
    """Why these arguments do not fit a tag's recorded parameters, or ``""``."""

    positional, keywords = _arguments(bits)
    keyword_only = parameters.index("*") if "*" in parameters else len(parameters)
    ordered = [name for name in parameters[:keyword_only] if not name.startswith("*") and name != "/"]
    names = {name.rstrip("=").lstrip("*") for name in parameters if name not in {"*", "/"}}
    if len(positional) > len(ordered) and not any(name.startswith("*") and name != "*" for name in parameters):
        return f"takes at most {len(ordered)} positional arguments"
    unknown = sorted(keywords - names)
    if unknown and not any(name.startswith("**") for name in parameters):
        return f"takes no {', '.join(unknown)}"
    given = set(ordered[: len(positional)]) | keywords
    missing = [
        name
        for name in parameters
        if name not in {"*", "/"} and not name.endswith("=") and not name.startswith("*") and name not in given
    ]
    return f"needs {', '.join(missing)}" if missing else ""


@dataclass
class _Template:
    """One extension template, read token by token against the contract."""

    name: str
    contract: dict[str, Any]
    own_templates: dict[str, Path]
    own_libraries: dict[str, frozenset[str]]
    problems: list[tuple[int, str]] = field(default_factory=list)
    frame: str | None = None
    depth: int = 0
    loaded: set[str] = field(default_factory=set)

    def problem(self, line: int, message: str) -> None:
        self.problems.append((line, message))

    def check(self, text: str) -> list[tuple[int, str]]:
        skipping = ""
        for token in Lexer(text).tokenize():
            if token.token_type not in (TokenType.BLOCK, TokenType.VAR):
                continue
            bits = token.split_contents()
            if skipping:
                skipping = "" if token.token_type == TokenType.BLOCK and bits[0] == skipping else skipping
                continue
            if token.token_type == TokenType.BLOCK and bits[0] in {"comment", "verbatim"}:
                skipping = f"end{bits[0]}"
                continue
            line = token.lineno or 0
            self._filters(token.contents, line)
            if token.token_type == TokenType.BLOCK:
                self._tag(bits, line)
        self._markup(text)
        return self.problems

    def _tag(self, bits: list[str], line: int) -> None:
        tag = bits[0]
        handler = {
            "extends": self._extends,
            "block": self._block,
            "endblock": self._endblock,
            "include": self._include,
            "load": self._load,
        }.get(tag)
        if handler is not None:
            handler(bits, line)
            return
        if tag.startswith("end") or tag in _CONTINUATIONS:
            return
        django = self.contract["django"]["tags"]
        if tag in self.contract["tags"]:
            problem = _signature_problem(self.contract["tags"][tag]["parameters"], bits[1:])
            if problem:
                self.problem(line, f"{{% {tag} %}} {problem}")
        elif tag not in django and tag not in self.loaded:
            self.problem(line, f"{{% {tag} %}} is not a public tag")

    def _extends(self, bits: list[str], line: int) -> None:
        parent = bits[1].strip("\"'") if len(bits) > 1 else ""
        if parent in self.contract["frames"]:
            self.frame = parent
        elif parent not in self.own_templates:
            self.problem(
                line, f"extends {parent}, which is not a public frame ({', '.join(sorted(self.contract['frames']))})"
            )

    def _block(self, bits: list[str], line: int) -> None:
        name = bits[1] if len(bits) > 1 else ""
        if self.frame and self.depth == 0 and name not in self.contract["frames"][self.frame]["blocks"]:
            public = ", ".join(sorted(self.contract["frames"][self.frame]["blocks"]))
            self.problem(line, f"overrides {{% block {name} %}} of {self.frame}, whose public blocks are {public}")
        self.depth += 1

    def _endblock(self, bits: list[str], line: int) -> None:
        self.depth -= 1

    def _include(self, bits: list[str], line: int) -> None:
        if len(bits) < 2 or bits[1][:1] not in {'"', "'"}:
            self.problem(line, "includes a template named at render time, which no check can hold to the contract")
            return
        name = bits[1].strip("\"'")
        partial = self.contract["partials"].get(name)
        if partial is None:
            if name not in self.own_templates:
                self.problem(line, f"includes {name}, which is not a public partial")
            return
        rest = bits[2:]
        given = {bit.partition("=")[0] for bit in rest[1:] if "=" in bit} if rest[:1] == ["with"] else set()
        for key in sorted(given - set(partial["required"]) - set(partial["optional"])):
            self.problem(line, f"passes {key} to {name}, which does not read it")
        for key in partial["required"]:
            if key not in given:
                self.problem(line, f"leaves {key} of {name} to the page's context; pass it by name")

    def _load(self, bits: list[str], line: int) -> None:
        libraries = bits[-1:] if "from" in bits else bits[1:]
        for library in libraries:
            if library in self.own_libraries:
                self.loaded |= self.own_libraries[library]
            elif library in self.contract["django"]["libraries"]:
                self.loaded |= set(self.contract["django"]["libraries"][library])
            else:
                self.problem(line, f"loads {library}, which is not a public library")

    def _filters(self, contents: str, line: int) -> None:
        for match in _FILTER.finditer(_QUOTED.sub('""', contents)):
            name, argued = match.group(1), bool(match.group(2))
            host = self.contract["filters"].get(name)
            if host is not None:
                argument = host["argument"]
                if (argument == "none" and argued) or (argument == "required" and not argued):
                    self.problem(line, f"|{name} takes {'no' if argument == 'none' else 'an'} argument")
            elif name not in self.contract["django"]["filters"] and name not in self.loaded:
                self.problem(line, f"|{name} is not a public filter")

    def _markup(self, text: str) -> None:
        # Each tag reads as one mark, keeping its line breaks so lines still count.
        markup = _TEMPLATE_TAG.sub(lambda tag: "\0" + "\n" * tag.group().count("\n"), text)
        classes = set(self.contract["classes"])
        hooks = set(self.contract["hooks"])
        for match in _CLASS.finditer(markup):
            line = markup.count("\n", 0, match.start()) + 1
            # A name built around a value (``pill-{{ tone }}``) is decided when
            # it renders; the whole names around it are still checked.
            for word in match.group(1).split():
                if "\0" not in word and word not in classes:
                    self.problem(line, f"class {word} is not public")
        for match in _HOOK.finditer(markup):
            if match.group(1) not in hooks:
                line = markup.count("\n", 0, match.start()) + 1
                self.problem(line, f"{match.group(1)} is not a public script hook")


def template_violations(source_root: str | Path, contract: dict[str, Any] | None = None) -> list[str]:
    """Return stable ``path:line: problem`` lines for templates outside the public contract."""

    root = Path(source_root).resolve()
    templates = (contract or json.loads(_CONTRACT.read_text(encoding="utf-8")))["templates"]
    own_templates = _own_templates(root)
    own_libraries = _own_libraries(root)
    found: list[tuple[str, int, str]] = []
    # A file is checked once, though a nested templates directory names it twice.
    checked: set[Path] = set()
    for name, path in sorted(own_templates.items()):
        if path in checked:
            continue
        checked.add(path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # an image or font kept beside the templates is no template
        reader = _Template(name, templates, own_templates, own_libraries)
        found.extend((path.relative_to(root).as_posix(), line, message) for line, message in reader.check(text))
    return [f"{path}:{line}: {message}" for path, line, message in sorted(found)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject plugin imports outside hq_sdk and templates outside the public template contract."
    )
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    if not any(args.source_root.rglob("*.py")):
        # A root that holds no package would pass every check by checking nothing.
        print(f"{args.source_root} holds no Python package to check.")
        return 2
    failed = False
    imports = unsupported_hq_imports(args.source_root)
    if imports:
        failed = True
        print("Unsupported HQ implementation imports; use hq_sdk instead:")
        for violation in imports:
            print(f"- {violation}")
    templates = template_violations(args.source_root)
    if templates:
        failed = True
        print("Templates outside HQ's public template contract (docs/Plugins.md, Shared UI contract):")
        for violation in templates:
            print(f"- {violation}")
    if failed:
        return 1
    print("HQ SDK import and template boundaries passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
