"""Static conformance checks for plugin source trees.

Each read without importing the plugin or configuring Django: its Python
imports only ``hq_sdk``, its templates use only the public pieces of the
host's (``contract.json``'s ``templates`` section), and its tests read a table
cell by its text, never by the markup the host closes it with.
"""

import argparse
import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

lazy from django.template.base import Lexer, TokenType

# The host's implementation packages: every top-level package of the host tree,
# which plugins reach through hq_sdk instead. The tree is read where the SDK sits
# beside it; `hq` is the one name that holds where it does not, so an SDK
# installed without its host still refuses the host's namespace.
_FLOOR = frozenset({"hq"})

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


# Test checks ---------------------------------------------------------------

# A cell's text is what a page promises; the markup around it is the host's,
# which may wrap what a cell says (a link, a mark, a toggle) without changing a
# word. So no test looks for text that ends on a cell's closing tag.
_CELL_CLOSE = ("</td>", "</th>")
# Each text assertion, with the position and keyword of the text it looks for.
_NEEDLES = {
    "assertContains": (1, "text"),
    "assertNotContains": (1, "text"),
    "assertIn": (0, "member"),
    "assertNotIn": (0, "member"),
}


def _literal_end(node: ast.expr) -> str:
    """The literal text an expression ends with; empty when its end is computed."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        return _literal_end(node.values[-1])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_end(node.right)
    return ""


def _needle(call: ast.Call) -> ast.expr | None:
    if not isinstance(call.func, ast.Attribute) or call.func.attr not in _NEEDLES:
        return None
    position, keyword = _NEEDLES[call.func.attr]
    if len(call.args) > position:
        return call.args[position]
    return next((named.value for named in call.keywords if named.arg == keyword), None)


def _cell_markup_pins(tree: ast.Module) -> list[int]:
    """The line of each text assertion in ``tree`` that looks for a cell's closing tag."""

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (needle := _needle(node)) is not None
        and _literal_end(needle).rstrip().endswith(_CELL_CLOSE)
    ]


def _pinned_cells(source_root: Path) -> list[str]:
    root = source_root.resolve()
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except OSError, SyntaxError:
            continue  # unsupported_hq_imports reports a file that does not parse
        found.extend(f"{path.relative_to(root)}:{line}" for line in _cell_markup_pins(tree))
    return found


# Sentence checks ------------------------------------------------------------

# A value HQ did not write (a registrar's name, a provider's phrase, a label
# someone typed) can carry its own period: "Example, Inc." closed with
# ``f"... {registrar}."`` reads "Example, Inc..". ``ui.ended`` adds the full
# stop only when the text has none, so no f-string that builds page text closes
# ``{value}.`` by hand. A value is left bare only where the source shows it is
# the host's own words: a ``!r`` or a format spec, an UPPER_CASE constant, one
# of the host's phrase functions, or a choice between two literals. The
# argument of a ``raise`` is left out: it reaches a log, not a page.

# HQ's own phrase functions: their words end on a literal HQ wrote.
_OWN_PHRASES = frozenset(
    {"counted", "ago", "duration", "span", "when", "when_day", "when_exact", "expiry_phrase", "human_bytes"}
)
# Builders whose template is markup, where ``ended`` cannot reach the text.
_MARKUP = frozenset({"format_html"})
# The helper itself, which is where the full stop is written.
_CLOSERS = frozenset({"ended"})
# String methods whose argument is a name's prefix or suffix ("noun."), never a sentence.
_AFFIXES = frozenset({"removeprefix", "removesuffix", "startswith", "endswith"})


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def _constant(node: ast.AST) -> bool:
    name = _node_name(node)
    return bool(name) and name.isupper()


def _hq_words(value: ast.AST) -> bool:
    """Whether the source shows the value is HQ's own words."""

    if isinstance(value, ast.FormattedValue):
        if value.conversion == ord("r") or value.format_spec is not None:
            return True
        value = value.value
    if isinstance(value, ast.Constant) or _constant(value):
        return True
    if isinstance(value, ast.Call):
        return _node_name(value.func) in _OWN_PHRASES or (
            isinstance(value.func, ast.Attribute) and _constant(value.func.value)
        )
    if isinstance(value, ast.IfExp):
        return all(isinstance(branch, ast.Constant) for branch in (value.body, value.orelse))
    return False


def _ends_on(node: ast.AST) -> list[ast.AST]:
    """The values text ends on: none when it ends on a literal HQ wrote."""

    if isinstance(node, ast.Constant):
        return []
    if isinstance(node, ast.JoinedStr):
        return _ends_on(node.values[-1]) if node.values else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _ends_on(node.right)
    if isinstance(node, ast.IfExp):
        return _ends_on(node.body) + _ends_on(node.orelse)
    return [node]


def _openings(node: ast.AST) -> set[str]:
    """The characters text can open with; "" where the source cannot say."""

    if isinstance(node, ast.Constant):
        return {str(node.value)[:1]}
    if isinstance(node, ast.JoinedStr):
        return _openings(node.values[0]) if node.values else {""}
    if isinstance(node, ast.FormattedValue):
        return _openings(node.value)
    if isinstance(node, ast.IfExp):
        return _openings(node.body) | _openings(node.orelse)
    return {""}


def _closes_sentence(text: str, after: ast.AST | None) -> bool:
    """Whether literal text right after a value ends a sentence there.

    "." at the end, or before a space or a value that opens with one; ".json"
    or the dot between two values of a hostname is no sentence end.
    """

    if not text.startswith("."):
        return False
    rest = text[1:]
    if rest:
        return rest[0].isspace() or (after is None and not rest.strip())
    return after is None or any(opening.isspace() for opening in _openings(after) if opening)


def _opens_on_a_close(node: ast.AST) -> bool:
    """Whether text opens by ending the sentence before it: ". Next", "."; ".part" does not."""

    if isinstance(node, ast.Constant):
        return _closes_sentence(str(node.value), None)
    if isinstance(node, ast.JoinedStr) and node.values:
        first, *rest = node.values
        if isinstance(first, ast.Constant):
            return _closes_sentence(str(first.value), rest[0] if rest else None)
        return isinstance(first, ast.FormattedValue) and _opens_on_a_close(first.value)
    if isinstance(node, ast.IfExp):
        return _opens_on_a_close(node.body) or _opens_on_a_close(node.orelse)
    return False


def _bare_closes(node: ast.AST) -> list[ast.AST]:
    """Each value a sentence closes on with a period written by hand."""

    if isinstance(node, ast.JoinedStr):
        parts = node.values
        afters: list[ast.expr | None] = [*parts[2:], None]
        return [
            value
            for value, following, after in zip(parts, parts[1:], afters, strict=False)
            if isinstance(value, ast.FormattedValue)
            and isinstance(following, ast.Constant)
            and _closes_sentence(str(following.value), after)
        ]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and _opens_on_a_close(node.right):
        return _ends_on(node.left)
    return []


def _template_close(node: ast.AST) -> bool:
    """A ``str.format`` template that closes a sentence on a field: "Saved: {target}."."""

    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.rstrip().endswith("}.")


def _left_out(tree: ast.AST) -> set[int]:
    """The nodes under a ``raise``, in a markup builder's or affix test's call, or in ``ended`` itself."""

    skipped: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Raise)
            or (isinstance(node, ast.Call) and _node_name(node.func) in _MARKUP | _AFFIXES)
            or (isinstance(node, ast.FunctionDef) and node.name in _CLOSERS)
        ):
            skipped.update(id(inner) for inner in ast.walk(node))
    return skipped


def _shown(value: ast.AST) -> str:
    return ast.unparse(value.value if isinstance(value, ast.FormattedValue) else value)


def _closes_by_hand(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, value)`` for each page sentence that closes on a value with a hand-written period."""

    skipped = _left_out(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skipped or not isinstance(node, ast.expr):
            continue
        found.extend((node.lineno, _shown(value)) for value in _bare_closes(node) if not _hq_words(value))
        if isinstance(node, ast.Constant) and _template_close(node):
            found.append((node.lineno, str(node.value)))
    return found


# Where page text is not built: a test, a migration, a management command's output.
_SENTENCE_LEFT_OUT = frozenset({"tests", "migrations", "management"})


def sentence_closes(source_root: str | Path) -> list[str]:
    """Return stable ``path:line: value`` lines for each page sentence closed on a value by hand."""

    root = Path(source_root).resolve()
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if _SENTENCE_LEFT_OUT & set(relative.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except OSError, SyntaxError:
            continue  # unsupported_hq_imports reports a file that does not parse
        found.extend(f"{relative}:{line}: {value}" for line, value in sorted(_closes_by_hand(tree)))
    return found


# Template checks -----------------------------------------------------------

_CONTRACT = Path(__file__).with_name("contract.json")
_REGISTERS = frozenset({"filter", "simple_tag", "inclusion_tag", "tag", "simple_block_tag"})
# Tags that continue or close another rather than standing alone.
_CONTINUATIONS = frozenset({"else", "elif", "empty", "plural"})
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_FILTER = re.compile(r"\|\s*(\w+)(\s*:)?")
_TEMPLATE_TAG = re.compile(r"\{[{%#].*?[}%#]\}", re.DOTALL)
_CLASS = re.compile(r"\bclass=\"([^\"]*)\"")
_LITERAL = re.compile(r"([\"'])(.*)\1")
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
    # The open host tags that have later parts, innermost last.
    enclosing: list[str] = field(default_factory=list)

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
            if self.enclosing and tag == f"end{self.enclosing[-1]}":
                self.enclosing.pop()
            return
        host = self.contract["tags"].get(tag)
        if host is not None:
            self._host_tag(tag, host, bits[1:], line)
        elif self.enclosing and tag in self.contract["tags"][self.enclosing[-1]]["parts"]:
            return
        elif tag not in self.contract["django"]["tags"] and tag not in self.loaded:
            self.problem(line, f"{{% {tag} %}} is not a public tag")

    def _host_tag(self, tag: str, host: dict[str, Any], bits: list[str], line: int) -> None:
        problem = _signature_problem(host["parameters"], bits)
        if problem:
            self.problem(line, f"{{% {tag} %}} {problem}")
        # A class handed over as an option is held as a class attribute is; one
        # given as a variable is decided when it renders.
        for bit in bits:
            option, _, value = bit.partition("=")
            literal = _LITERAL.fullmatch(value)
            if option in host["classes"] and literal:
                self._classes(literal.group(2).split(), line)
        # A later part ({% aside %}) is written only inside its own tag.
        if host["parts"]:
            self.enclosing.append(tag)

    def _classes(self, words: list[str], line: int) -> None:
        public = self.contract["classes"]
        for word in words:
            if word not in public:
                self.problem(line, f"class {word} is not public")

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

    def _endblock(self, _bits: list[str], _line: int) -> None:
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


def _report(heading: str, violations: list[str]) -> bool:
    """Print one check's violations under its heading; whether there were any."""

    if violations:
        print(heading)
        for violation in violations:
            print(f"- {violation}")
    return bool(violations)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reject plugin imports outside hq_sdk, templates outside the public template contract,"
            " tests that pin a table cell's closing tag, and page sentences closed on a value by hand."
        )
    )
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    if not any(args.source_root.rglob("*.py")):
        # A root that holds no package would pass every check by checking nothing.
        print(f"{args.source_root} holds no Python package to check.")
        return 2
    refused = [
        _report("Unsupported HQ implementation imports; use hq_sdk instead:", unsupported_hq_imports(args.source_root)),
        _report(
            "Templates outside HQ's public template contract (docs/Plugins.md, Shared UI contract):",
            template_violations(args.source_root),
        ),
        _report(
            "Tests that look for a table cell's closing tag, which breaks when the host wraps what the cell says;"
            " read the cell's text instead (its td or th parsed, whitespace collapsed):",
            _pinned_cells(args.source_root),
        ),
        _report(
            'Page sentences closed on a value with a hand-written period; write ended(f"... {value}")'
            " from hq_sdk.ui, which keeps a value's own period:",
            sentence_closes(args.source_root),
        ),
    ]
    if any(refused):
        return 1
    print("HQ SDK import, template, table cell and sentence boundaries passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
