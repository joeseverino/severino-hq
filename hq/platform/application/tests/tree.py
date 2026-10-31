"""Which tests of a checkout carry which tags, and which of them walk a tree, read from their source.

The guard in test_tree_tag.py holds the ``tree`` tag to the walks, and
``mise run fast`` runs the tests it finds tagged ``tree``.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from hq.platform.application.tests.source import parsed, source_files

ROOT = Path(__file__).resolve().parents[4]
SUITE = ("hq", "hq_sdk", "tools", "tests")
SETUP = ("setUp", "setUpClass", "setUpTestData")

# A definition found by name: the module it lives in and its node.
Found = tuple[str, ast.AST]


def is_walk(node: ast.AST) -> bool:
    """``x.rglob(...)``, ``os.walk(...)``, or ``ls-files`` in a string."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "ls-files" in node.value.split()
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    owner = node.func.value
    return node.func.attr == "rglob" or (node.func.attr == "walk" and isinstance(owner, ast.Name) and owner.id == "os")


def tags(decorators: list[ast.expr]) -> set[str]:
    found: set[str] = set()
    for decorator in decorators:
        if (
            isinstance(decorator, ast.Call)
            and getattr(decorator.func, "id", getattr(decorator.func, "attr", "")) == "tag"
        ):
            found.update(arg.value for arg in decorator.args if isinstance(arg, ast.Constant))
    return found


class Module:
    """One module's top-level names: what each defines, and what each imports."""

    def __init__(self, dotted: str, path: Path):
        package = dotted if path.name == "__init__.py" else dotted.rpartition(".")[0]
        self.defined: dict[str, ast.AST] = {}
        # A local name -> (the module it came from, the name there, or None for the module itself).
        self.imported: dict[str, tuple[str, str | None]] = {}
        for node in parsed(path).body:
            self.read(node, package)

    def read(self, node: ast.stmt, package: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.defined[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            self.defined.update((target.id, node.value) for target in targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.partition(".")[0]
                self.imported[local] = (alias.name if alias.asname else local, None)
        elif isinstance(node, ast.ImportFrom):
            head = node.module or ""
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                head = ".".join([*base, *([node.module] if node.module else [])])
            for alias in node.names:
                self.imported[alias.asname or alias.name] = (head, alias.name)


@dataclass(frozen=True)
class Unit:
    """One test method: its label, its tags and where its own tags stand."""

    label: str
    tags: frozenset[str]
    on_method: bool
    walks: bool


class Walks:
    """Which definitions of a checkout reach a walk, followed by name across its modules."""

    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, Module | None] = {}
        self.known: dict[int, bool] = {}

    def module(self, dotted: str) -> Module | None:
        if dotted not in self.modules:
            base = self.root.joinpath(*dotted.split("."))
            path = next((p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None)
            self.modules[dotted] = Module(dotted, path) if path and dotted.split(".", maxsplit=1)[0] in SUITE else None
        return self.modules[dotted]

    def resolve(self, dotted: str, name: str) -> Found | str | None:
        """What ``name`` means in a module: a definition, a module's dotted name, or nothing of this checkout."""

        module = self.module(dotted)
        if module is None:
            return None
        if name in module.defined:
            return dotted, module.defined[name]
        if name not in module.imported:
            return None
        source, attribute = module.imported[name]
        if attribute is None or self.module(f"{source}.{attribute}"):
            return source if attribute is None else f"{source}.{attribute}"
        return self.resolve(source, attribute)

    def member(self, dotted: str, cls: ast.ClassDef, name: str) -> Found | None:
        """A method or class attribute, looked up through the bases this checkout defines."""

        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return dotted, node
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
                return dotted, node.value
        for base in cls.bases:
            found = self.resolve(dotted, base.id) if isinstance(base, ast.Name) else None
            if isinstance(found, tuple) and isinstance(found[1], ast.ClassDef):
                inherited = self.member(found[0], found[1], name)
                if inherited:
                    return inherited
        return None

    def definition(self, found: Found, cls: ast.ClassDef | None = None) -> bool:
        """Whether a definition walks; a class is its constructor. A cycle counts as no walk."""

        dotted, node = found
        if isinstance(node, ast.ClassDef):
            init = self.member(dotted, node, "__init__")
            return bool(init) and self.definition(init, node)
        if id(node) not in self.known:
            self.known[id(node)] = False
            self.known[id(node)] = self.reaches(dotted, node, cls)
        return self.known[id(node)]

    def reaches(self, dotted: str, node: ast.AST, cls: ast.ClassDef | None) -> bool:
        return any(self.step(dotted, sub, cls) for sub in ast.walk(node))

    def step(self, dotted: str, node: ast.AST, cls: ast.ClassDef | None) -> bool:
        """Whether one node walks, or names something that does."""

        if is_walk(node):
            return True
        found: Found | str | None = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found = self.resolve(dotted, node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if cls and node.value.id in ("self", "cls"):
                found = self.member(dotted, cls, node.attr)
            elif isinstance(owner := self.resolve(dotted, node.value.id), str):
                found = self.resolve(owner, node.attr)
        return isinstance(found, tuple) and self.definition(found, cls)

    def units(self, dotted: str) -> list[Unit]:
        """Every test method of a test module, with the tags Django gives it.

        A class that replaces the ROOT of the code it drives runs that code on
        a fixture, so what it walks is the fixture, never this checkout.
        """

        found = []
        for cls, method, held, own in self.methods(dotted):
            setup = [m for name in SETUP if (m := self.member(dotted, cls, name))]
            if replaces_root([method, *(node for _, node in setup)]):
                walks = False
            else:
                walks = any(self.definition(m, cls) for m in setup) or self.definition((dotted, method), cls)
            found.append(Unit(f"{dotted}.{cls.name}.{method.name}", frozenset(held | own), bool(own), walks))
        return found

    def methods(self, dotted: str) -> Iterator[tuple[ast.ClassDef, ast.FunctionDef, set[str], set[str]]]:
        """Each test method of a test module: its class, itself, its class's tags and its own."""

        module = self.module(dotted)
        assert module, dotted
        for cls in (node for node in module.defined.values() if isinstance(node, ast.ClassDef)):
            held = self.class_tags(dotted, cls)
            for method in cls.body:
                if isinstance(method, ast.FunctionDef) and method.name.startswith("test"):
                    yield cls, method, held, tags(method.decorator_list)

    def class_tags(self, dotted: str, cls: ast.ClassDef) -> set[str]:
        held = tags(cls.decorator_list)
        for base in cls.bases:
            found = self.resolve(dotted, base.id) if isinstance(base, ast.Name) else None
            if isinstance(found, tuple) and isinstance(found[1], ast.ClassDef):
                held |= self.class_tags(found[0], found[1])
        return held


def replaces_root(nodes: list[ast.AST]) -> bool:
    """Whether any of ``nodes`` patches a module's ROOT: ``patch.object(module, "ROOT", ...)`` or ``patch("a.b.ROOT", ...)``."""

    for node in (sub for each in nodes for sub in ast.walk(each)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        named = [arg.value for arg in node.args[:2] if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        if (name == "object" and named == ["ROOT"]) or (name == "patch" and named[:1] and named[0].endswith(".ROOT")):
            return True
    return False


def tagged(root: Path, tag: str) -> list[str]:
    """The label of every test carrying ``tag``: its class's, when every test of the class does."""

    walks = Walks(root)
    labels = []
    for dotted in test_modules(root):
        by_class: dict[str, list[tuple[str, bool]]] = {}
        for cls, method, held, own in walks.methods(dotted):
            by_class.setdefault(f"{dotted}.{cls.name}", []).append((method.name, tag in held | own))
        for cls, methods in by_class.items():
            if all(carries for _, carries in methods):
                labels.append(cls)
            else:
                labels += [f"{cls}.{name}" for name, carries in methods if carries]
    return labels


def test_modules(root: Path) -> list[str]:
    paths = [path for package in SUITE for path in source_files(root / package, "test*.py")]
    return [".".join(path.relative_to(root).with_suffix("").parts) for path in paths]


def misplaced(root: Path) -> tuple[list[str], list[str]]:
    """(source tests that walk without ``tree``, tree tags on no walk).

    A tag on a class asks only that one of its tests walk, so a class keeps
    one tag rather than one per method.
    """

    walks = Walks(root)
    untagged, idle = [], []
    for dotted in test_modules(root):
        by_class: dict[str, list[Unit]] = {}
        for unit in walks.units(dotted):
            by_class.setdefault(unit.label.rpartition(".")[0], []).append(unit)
            if unit.walks and "source" in unit.tags and "tree" not in unit.tags:
                untagged.append(unit.label)
        for cls, units in by_class.items():
            idle.extend(unit.label for unit in units if unit.on_method and "tree" in unit.tags and not unit.walks)
            if all("tree" in unit.tags and not unit.walks for unit in units) and any(not u.on_method for u in units):
                idle.append(cls)
    return untagged, idle
