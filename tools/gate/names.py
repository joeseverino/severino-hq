"""What a change reaches through the names modules refer to: the top-level definitions it edits and those
that refer to them (tools/gate/selection.py ``reached``, docs/Development.md, What `fast` runs)."""

from __future__ import annotations

import ast
import functools
import re
import subprocess
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from tools.gate import chain


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class Statement(NamedTuple):
    """One top-level statement of a module: the names it binds there, the names of this tree it refers to
    (a module and a name in it, or None for the module itself as a value), whether a decorator of this tree
    takes what it defines, and whether it is an import, which only binds."""

    binds: frozenset[str]
    refs: frozenset[tuple[str, str | None]]
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

    first = tree.body[:1]
    docstring = first and isinstance(first[0], ast.Expr) and isinstance(first[0].value, ast.Constant)
    return tree.body[1:] if docstring else tree.body


def outline(tree: ast.Module, path: Path, modules: frozenset[str]) -> tuple[Statement, ...]:
    """A module's top-level statements, as ``Statement`` reads each. A module under a tests directory also
    refers to the dotted names its strings spell (a mock target)."""

    module = module_name(path)
    here = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    statements = body(tree)
    top = {name: target for node in statements for name, target in bindings([node], here, modules).items()}
    defined = {name for node in statements for name in binds(node)} - top.keys()
    found = []
    for node in statements:
        refs = Refs(top | bindings(ast.walk(node), here, modules), defined, module, modules, "tests" in path.parts)
        refs.visit(node)
        imports = isinstance(node, ast.Import | ast.ImportFrom)
        if imports:
            refs.found |= {target for target in bindings([node], here, modules).values() if target[1]}
        decorated = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and any(
            refs.of(decorator) for decorator in node.decorator_list
        )
        found.append(Statement(binds(node), frozenset(refs.found), decorated, imports))
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


def changes(was: str | None, now: str) -> tuple[frozenset[str], bool]:
    """(the top-level names whose statements differ between a module's two texts, whether a statement that
    binds none does). Comments and the docstring are no part of either."""

    def shapes(text: str | None) -> tuple[dict[str, list[str]], Counter[str]]:
        named: dict[str, list[str]] = {}
        bare: Counter[str] = Counter()
        for node in body(ast.parse(text)) if text is not None else []:
            shape = ast.dump(node)
            for name in binds(node) or [""]:
                if name:
                    named.setdefault(name, []).append(shape)
                else:
                    bare[shape] += 1
        return named, bare

    (named_was, bare_was), (named_now, bare_now) = shapes(was), shapes(now)
    names = named_was.keys() | named_now.keys()
    return frozenset(n for n in names if named_was.get(n) != named_now.get(n)), bare_was != bare_now


class Reach(NamedTuple):
    """What a change reaches through the names modules refer to: each module one of whose statements it
    reaches, with the names those bind, and the modules whose reached statement is reached only as the
    module loads (``dynamic``)."""

    names: dict[str, set[str]]
    dynamic: set[str]


def name_reach(
    seeds: dict[str, tuple[frozenset[str], bool]], every: dict[str, tuple[Statement, ...]], exempt: set[str]
) -> Reach:
    """The statements ``seeds`` reach (each module, the names changed in it, whether a statement binding
    none changed), and those that refer to a name a reached one binds, all the way. A reached statement is
    reached only as its module loads when it binds nothing, a decorator of this tree takes it (which may
    register it) or nothing else refers to what it binds; ``exempt`` modules (a test, a URL module, which
    the runner and the routes reach) never are."""

    users: dict[tuple[str, str | None], set[tuple[str, int]]] = {}
    for module, statements in every.items():
        for i, statement in enumerate(statements):
            for ref in statement.refs:
                users.setdefault(ref, set()).add((module, i))
    names: dict[str, set[str]] = {module: set() for module in seeds}
    dynamic = {module for module, (_, bare) in seeds.items() if bare} - exempt
    queue: list[tuple[str, str]] = []
    done: set[tuple[str, int]] = set()

    def touch(module: str, i: int) -> None:
        if (module, i) in done:
            return
        done.add((module, i))
        statement = every[module][i]
        others = users.get((module, None)) or any(
            users.get((module, n), set()) - {(module, i)} for n in statement.binds
        )
        if (
            module not in exempt
            and not statement.imports
            and (not statement.binds or statement.decorated or not others)
        ):
            dynamic.add(module)
        names.setdefault(module, set())
        queue.extend((module, name) for name in statement.binds - names[module])
        names[module] |= statement.binds

    for module, (changed, bare) in seeds.items():
        for i, statement in enumerate(every.get(module, ())):
            if bare or statement.binds & changed:
                touch(module, i)
    valued: set[str] = set()
    while queue:
        module, name = queue.pop()
        hits = set(users.get((module, name), ()))
        if module not in valued:
            valued.add(module)
            hits |= users.get((module, None), set())
        for user in hits:
            touch(*user)
    return Reach(names, dynamic)


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
