"""What a change reaches through the names modules refer to: the top-level definitions it edits and those
that refer to them (tools/gate/selection.py ``reached``, docs/Development.md, What `fast` runs)."""

from __future__ import annotations

import ast
import copy
import functools
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

from tools.gate import chain


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class Unit(NamedTuple):
    """A function's body (a method's by its class's name, ``Page.get``), which runs only when it is called:
    the names of this tree it refers to, and whether a decorator keeps what it answers (a cache), so a test
    that never runs it again may still read what it once answered."""

    name: str
    refs: frozenset[tuple[str, str | None]]
    cached: bool


class Statement(NamedTuple):
    """One top-level statement of a module: the names it binds there, the names of this tree it refers to as
    its module loads (a module and a name in it, or None for the module itself as a value), those it calls
    then (a call, a decorator), those only its function's signature names as types (``hints``), which
    Python evaluates only when something asks for them, its functions' bodies, whether a decorator of this
    tree takes what it defines, and whether it is an import, which only binds."""

    binds: frozenset[str]
    refs: frozenset[tuple[str, str | None]]
    calls: frozenset[tuple[str, str | None]]
    hints: frozenset[tuple[str, str | None]]
    units: tuple[Unit, ...]
    decorated: bool
    imports: bool


def outlines(files: list[Path]) -> dict[str, tuple[Statement, ...]]:
    """module -> its top-level statements, read once per tree and file list."""

    return _outlines(chain.ROOT, tuple(files))


@functools.cache
def _outlines(root: Path, files: tuple[Path, ...]) -> dict[str, tuple[Statement, ...]]:
    modules = frozenset(module_name(p) for p in files)
    return {
        module_name(path): outline(ast.parse((root / path).read_text(encoding="utf-8")), path, modules)
        for path in files
    }


def body(tree: ast.Module) -> list[ast.stmt]:
    """A module's top-level statements but its docstring."""

    return tree.body[1:] if docstring(tree.body) else tree.body


def outline(tree: ast.Module, path: Path, modules: frozenset[str]) -> tuple[Statement, ...]:
    """A module's top-level statements, as ``Statement`` reads each. A module under a tests directory also
    refers to the dotted names its strings spell (a mock target)."""

    module = module_name(path)
    here = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    statements = body(tree)
    top = {name: target for node in statements for name, target in bindings([node], here, modules).items()}
    defined = {name for node in statements for name in binds(node)} - top.keys()
    # A name an import binds from outside this tree (``dataclass``): no decorator of this tree.
    outside = {
        (module, name) for node in statements if isinstance(node, ast.Import | ast.ImportFrom) for name in binds(node)
    }
    found = []
    for node in statements:
        scope = top | bindings(ast.walk(node), here, modules)
        strings = "tests" in path.parts
        refs = Loading(scope, defined, module, modules, strings)
        refs.visit(node)
        imports = isinstance(node, ast.Import | ast.ImportFrom)
        if imports:
            refs.found |= {target for target in bindings([node], here, modules).values() if target[1]}
        decorated = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and any(
            refs.of(decorator) - outside for decorator in node.decorator_list
        )
        units = []
        for name, function in functions([node]):
            ran = Refs(scope, defined, module, modules, strings)
            for each in [*function.body, *([function.args, function.returns] if "." in name else [])]:
                if each is not None:
                    ran.visit(each)
            cached = any(MEMOS & set(dotted_words(d)) for d in function.decorator_list)
            units.append(Unit(name, frozenset(ran.found), cached))
        calls = frozenset(refs.calls)
        hints = frozenset(refs.hints) - refs.found - calls
        found.append(
            Statement(
                binds(node),
                frozenset(refs.found) - calls,
                calls,
                hints,
                tuple(units),
                decorated,
                imports,
            )
        )
    return tuple(found)


def binds(node: ast.stmt) -> frozenset[str]:
    """The names a top-level statement binds at the top level: what it defines, assigns or imports, and what
    a block there (an ``if``, a ``try``, a ``with``) does."""

    found: set[str] = set()
    blocks: list[list[ast.stmt]] = [[node]]
    while blocks:
        for each in blocks.pop():
            if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                found.add(each.name)
            elif isinstance(each, ast.Import | ast.ImportFrom):
                found |= {alias.asname or alias.name.split(".")[0] for alias in each.names if alias.name != "*"}
            elif isinstance(each, ast.Assign):
                found |= {n.id for t in each.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            elif isinstance(each, ast.AnnAssign | ast.AugAssign | ast.TypeAlias):
                target = each.name if isinstance(each, ast.TypeAlias) else each.target
                found |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
            elif isinstance(each, ast.If | ast.For | ast.While):
                blocks += [each.body, each.orelse]
            elif isinstance(each, ast.Try | ast.TryStar):
                blocks += [each.body, each.orelse, each.finalbody, *(h.body for h in each.handlers)]
            elif isinstance(each, ast.With):
                blocks.append(each.body)
    return frozenset(found)


def bindings(nodes: Iterable[ast.AST], here: list[str], modules: frozenset[str]) -> dict[str, tuple[str, str | None]]:
    """Each name the imports among ``nodes`` bind: (the module, the name in it, or None when it binds the
    module). A star import is bound by a name of its own, as a use of the whole module."""

    found: dict[str, tuple[str, str | None]] = {}
    for each in nodes:
        if isinstance(each, ast.Import):
            for alias in each.names:
                target = alias.name if alias.asname else alias.name.split(".")[0]
                found[alias.asname or target] = (target, None)
        elif isinstance(each, ast.ImportFrom):
            base = here[: len(here) - each.level + 1] if each.level else []
            source = ".".join([*base, *([each.module] if each.module else [])])
            for alias in each.names:
                if alias.name == "*":
                    found[f"*{source}"] = (source, None)
                elif f"{source}.{alias.name}" in modules:
                    found[alias.asname or alias.name] = (f"{source}.{alias.name}", None)
                elif source in modules:
                    found[alias.asname or alias.name] = (source, alias.name)
    return found


# A dotted name a string spells (a mock target), which names a module or a name in one.
DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")


class Refs(ast.NodeVisitor):
    """The names of this tree a statement refers to: a name an import binds, through the attributes read
    off it (``inbox.recent``), and a name the module defines. A name a function binds that shadows one is
    read as the module's, which only reaches further."""

    def __init__(
        self,
        scope: dict[str, tuple[str, str | None]],
        defined: set[str],
        module: str,
        modules: frozenset[str],
        strings: bool,
    ):
        self.scope, self.defined, self.module, self.modules, self.strings = scope, defined, module, modules, strings
        self.found = {target for name, target in scope.items() if name.startswith("*")}

    def of(self, node: ast.AST) -> set[tuple[str, str | None]]:
        other = Refs(self.scope, self.defined, self.module, self.modules, self.strings)
        other.found = set()
        other.visit(node)
        return other.found

    def dotted(self, pieces: list[str]) -> None:
        for n in range(len(pieces), 0, -1):
            if (prefix := ".".join(pieces[:n])) in self.modules:
                self.found.add((prefix, pieces[n] if n < len(pieces) else None))
                return

    def bound(self, name: str, rest: list[str]) -> None:
        module, attribute = self.scope[name]
        self.dotted([*module.split("."), *([attribute] if attribute else []), *rest])

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.scope:
            self.bound(node.id, [])
        elif node.id in self.defined:
            self.found.add((self.module, node.id))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        rest: list[str] = []
        value: ast.expr = node
        while isinstance(value, ast.Attribute):
            rest.insert(0, value.attr)
            value = value.value
        if isinstance(value, ast.Name) and value.id in self.scope:
            self.bound(value.id, rest)
        else:
            self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if self.strings and isinstance(node.value, str) and DOTTED.fullmatch(node.value):
            self.dotted(node.value.split("."))


class Loading(Refs):
    """The names a statement refers to as its module loads, a function's body left out (``Unit``), and
    those of them it calls then: a call's callee, a decorator."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: set[tuple[str, str | None]] = set()
        self.hints: set[tuple[str, str | None]] = set()
        self.classes = 0

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.calls |= self.of(decorator)
        if self.classes:
            # A method's signature is read only as it is called (``Unit``).
            return
        arguments = node.args
        for default in [*arguments.defaults, *filter(None, arguments.kw_defaults)]:
            self.visit(default)
        every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
        for hint in [*(a.annotation for a in every if a and a.annotation), node.returns]:
            if hint is not None:
                self.hints |= self.of(hint)
        if docstring(node.body):
            self.visit(node.body[0])

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.calls |= self.of(decorator)
        self.classes += 1
        self.generic_visit(node)
        self.classes -= 1

    def visit_Call(self, node: ast.Call) -> None:
        self.calls |= self.of(node.func)
        self.generic_visit(node)


# The decorators that keep what a function answers, so a test may read it without running the function.
MEMOS = frozenset({"cache", "lru_cache", "cached_property"})
# The decorators that make a method an attribute of another kind and register it nowhere: a method so
# decorated is its body alone, as an undecorated one is.
DESCRIPTORS = MEMOS | {"property", "setter", "getter", "deleter", "staticmethod", "classmethod", "override"}


def dotted_words(node: ast.expr) -> list[str]:
    """The names an expression spells (``functools.lru_cache(maxsize=1)`` spells functools and lru_cache)."""

    return [
        n.id if isinstance(n, ast.Name) else n.attr for n in ast.walk(node) if isinstance(n, ast.Name | ast.Attribute)
    ]


def docstring(statements: list[ast.stmt]) -> bool:
    first = statements[:1]
    return bool(first) and isinstance(first[0], ast.Expr) and isinstance(first[0].value, ast.Constant)


def functions(statements: list[ast.stmt], prefix: str = "") -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Each function among ``statements`` and the classes and blocks there, by its name under its classes'
    (``Page.get``); one defined in a function's body is part of that body."""

    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for each in statements:
        if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append((prefix + each.name, each))
        elif isinstance(each, ast.ClassDef):
            found += functions(each.body, f"{prefix}{each.name}.")
        elif isinstance(each, ast.If | ast.For | ast.While):
            found += functions(each.body + each.orelse, prefix)
        elif isinstance(each, ast.Try | ast.TryStar):
            found += functions(
                each.body + each.orelse + each.finalbody + [s for h in each.handlers for s in h.body], prefix
            )
        elif isinstance(each, ast.With):
            found += functions(each.body, prefix)
    return found


class Edit(NamedTuple):
    """What changed in a module between two texts: the top-level names whose statements differ as the
    module loads them (a function's body left out), whether a statement that binds none does, and the
    function bodies that differ (``Unit``). Comments and the module's docstring are no part of any."""

    names: frozenset[str]
    bare: bool
    units: frozenset[str]

    def joined(self, other: Edit) -> Edit:
        return Edit(self.names | other.names, self.bare or other.bare, self.units | other.units)


def changes(was: str | None, now: str) -> Edit:
    def shapes(text: str | None) -> tuple[dict[str, list[str]], Counter[str], dict[str, list[str]]]:
        named: dict[str, list[str]] = {}
        bare: Counter[str] = Counter()
        units: dict[str, list[str]] = {}
        for node in body(ast.parse(text)) if text is not None else []:
            for name, function in functions([node]):
                # A method's signature is its body's: Python reads neither as its class loads.
                parts = [*function.body, function.args, function.returns] if "." in name else function.body
                units.setdefault(name, []).append(" ".join(ast.dump(part) for part in parts if part is not None))
            shape = ast.dump(ast.Module(loaded(copy.deepcopy([node])), []))
            for name in binds(node) or [""]:
                if name:
                    named.setdefault(name, []).append(shape)
                else:
                    bare[shape] += 1
        return named, bare, units

    (named_was, bare_was, units_was), (named_now, bare_now, units_now) = shapes(was), shapes(now)
    names = {n for n in named_was.keys() | named_now.keys() if named_was.get(n) != named_now.get(n)}
    units = {n for n in units_was.keys() | units_now.keys() if units_was.get(n) != units_now.get(n)}
    # A method added may override one its class's instances answered with: what ran any method of the
    # class, or what refers to a class that has no other. One taken away reaches what ran it.
    for method in units_now.keys() - units_was.keys():
        if "." in method:
            owner = method.rpartition(".")[0]
            others = {u for u in units_now if u.rpartition(".")[0] == owner} - {method}
            units |= others
            if not others:
                names.add(method.partition(".")[0])
    return Edit(frozenset(names), bare_was != bare_now, frozenset(units))


def loaded(statements: list[ast.stmt], in_class: bool = False) -> list[ast.stmt]:
    """``statements`` as their module loads them: a function's body left out but its docstring, and a
    method all but its decorators, which its class runs."""

    kept: list[ast.stmt] = []
    for each in statements:
        if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef):
            if in_class:
                if all(DESCRIPTORS & set(dotted_words(d)) for d in each.decorator_list):
                    continue
                each.args, each.returns, each.body = ast.arguments(), None, []
            else:
                each.body = each.body[:1] if docstring(each.body) else []
        elif isinstance(each, ast.ClassDef):
            each.body = loaded(each.body, in_class=True)
        elif isinstance(each, ast.If | ast.For | ast.While):
            each.body, each.orelse = loaded(each.body, in_class), loaded(each.orelse, in_class)
        elif isinstance(each, ast.Try | ast.TryStar):
            each.body, each.orelse = loaded(each.body, in_class), loaded(each.orelse, in_class)
            each.finalbody = loaded(each.finalbody, in_class)
            for handler in each.handlers:
                handler.body = loaded(handler.body, in_class)
        elif isinstance(each, ast.With):
            each.body = loaded(each.body, in_class)
        kept.append(each)
    return kept


class Reach(NamedTuple):
    """What a change reaches through the names modules refer to: each module one of whose statements it
    reaches as the module loads, with the names those bind; the modules whose reached statement is reached
    only as the module loads (``dynamic``); and the function bodies it reaches in the modules a record of
    what each test ran holds (``ran``), by module, which reach the tests that ran them."""

    names: dict[str, set[str]]
    dynamic: set[str]
    ran: dict[str, set[str]]


# How a statement refers to a name: as its module loads, by calling it then, or from a function's body
# (the index of its ``Unit``).
LOADS, CALLS, HINTS = -2, -1, -3


def name_reach(
    seeds: dict[str, Edit],
    every: dict[str, tuple[Statement, ...]],
    exempt: set[str],
    recorded: Mapping[str, frozenset[str]] | None = None,
) -> Reach:
    """The statements and function bodies ``seeds`` reach, and those that refer to a name a reached
    statement binds, all the way.

    A function body in a ``recorded`` module (one a record of what each test ran holds, with the bodies
    that ran as the suite started) is answered by that record: the tests that ran it ran what it calls, so
    the walk goes on from it only to what calls it as a module loads. Elsewhere, and for a body whose answer
    a cache keeps or that ran as the suite started, a reached body reaches its statement. A reached statement is reached only as its module loads when it binds nothing, a decorator of
    this tree takes it (which may register it) or nothing else refers to what it binds; ``exempt`` modules
    (a test, a URL module, which the runner and the routes reach) never are, and their bodies reach their
    statements."""

    recorded = recorded or {}
    users: dict[tuple[str, str | None], set[tuple[str, int, int]]] = {}
    for module, statements in every.items():
        for i, statement in enumerate(statements):
            for part, refs in ((LOADS, statement.refs), (CALLS, statement.calls), (HINTS, statement.hints)):
                for ref in refs:
                    users.setdefault(ref, set()).add((module, i, part))
            for j, unit in enumerate(statement.units):
                for ref in unit.refs:
                    users.setdefault(ref, set()).add((module, i, j))
    names: dict[str, set[str]] = {}
    ran: dict[str, set[str]] = {}
    dynamic = {module for module, edit in seeds.items() if edit.bare} - exempt
    # A name, and whether its value changed or only a body of the statement that binds it.
    queue: list[tuple[str, str, bool]] = []
    done: set[tuple[str, int]] = set()
    called: set[tuple[str, int]] = set()

    def touch(module: str, i: int) -> None:
        if (module, i) in done:
            return
        done.add((module, i))
        statement = every[module][i]
        others = users.get((module, None)) or any(
            {(m, n) for m, n, _ in users.get((module, b), ())} - {(module, i)} for b in statement.binds
        )
        if (
            module not in exempt
            and not statement.imports
            and (not statement.binds or statement.decorated or not others)
        ):
            dynamic.add(module)
        names.setdefault(module, set())
        queue.extend((module, name, True) for name in statement.binds - names[module])
        names[module] |= statement.binds

    def hit(module: str, i: int, j: int) -> None:
        unit = every[module][i].units[j]
        if module in exempt or module not in recorded or unit.cached or unit.name in recorded[module]:
            touch(module, i)
            return
        ran.setdefault(module, set()).add(unit.name)
        bodies(module, i)

    def bodies(module: str, i: int) -> None:
        """Go on from a statement only a body of which changed: to what calls it as a module loads."""

        if (module, i) not in called:
            called.add((module, i))
            queue.extend((module, name, False) for name in every[module][i].binds)

    for module, edit in seeds.items():
        for i, statement in enumerate(every.get(module, ())):
            if edit.bare or statement.binds & edit.names:
                touch(module, i)
            for j, unit in enumerate(statement.units):
                if unit.name in edit.units:
                    hit(module, i, j)
        # A body taken away: what ran it, as the record has it.
        gone = edit.units - {unit.name for statement in every.get(module, ()) for unit in statement.units}
        if gone and module in recorded and module not in exempt:
            ran.setdefault(module, set()).update(gone)
    valued: set[tuple[str, bool]] = set()
    while queue:
        module, name, value = queue.pop()
        found = set(users.get((module, name), ()))
        if (module, value) not in valued:
            valued.add((module, value))
            found |= users.get((module, None), set())
        for user, i, part in found:
            if part >= 0:
                if value:
                    hit(user, i, part)
            elif every[user][i].imports and not value:
                # An import that binds it again: what calls it there, as its module loads.
                bodies(user, i)
            elif part == HINTS:
                # A function whose signature names a changed type answers as it did, called; only what
                # reads its signature sees the change, as a registry that takes it may (a decorator).
                if value and every[user][i].decorated:
                    touch(user, i)
            elif value or part == CALLS:
                touch(user, i)
    return Reach(names, dynamic, ran)


def at_base(base: str, paths: list[str]) -> dict[str, str]:
    """The text each of ``paths`` holds at ``base``, for those it holds, read in one git call."""

    if not paths:
        return {}
    asked = "".join(f"{base}:{path}\n" for path in paths).encode()
    out = subprocess.run(["git", "cat-file", "--batch"], cwd=chain.ROOT, input=asked, capture_output=True, check=True)
    found: dict[str, str] = {}
    at = 0
    for path in paths:
        end = out.stdout.index(b"\n", at)
        header = out.stdout[at:end].split()
        at = end + 1
        if header[-1] != b"missing":
            size = int(header[2])
            found[path] = out.stdout[at : at + size].decode("utf-8")
            at += size + 1
    return found
